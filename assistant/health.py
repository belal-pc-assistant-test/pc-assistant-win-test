"""/health: a read-only PC check (Round 9). It changes nothing and needs no approval.

Each item is probed on its own. A probe that fails, or a missing feature, gives
``unknown`` with a short reason; it never raises. Output carries only drive letters,
numbers, service states and on/off flags: no user names, paths or serial numbers.
"""
from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from . import winshell
from .actions.base import ActionError, run_powershell

PROBE_TIMEOUT = 30
LOW_DISK_FRACTION = 0.10
LOW_MEMORY_FRACTION = 0.10
LONG_UPTIME_DAYS = 14
OLD_SIGNATURE_DAYS = 3
GIB = 1024 ** 3

OK, WARN, UNKNOWN = "ok", "warn", "unknown"
MARKERS = {OK: "✅", WARN: "⚠️", UNKNOWN: "❔"}
# Verdict words. Arabic is used only when the user's saved language is Arabic.
VERDICT = {
    "en": {OK: "OK", WARN: "WARNING", UNKNOWN: "UNKNOWN"},
    "ar": {OK: "سليم", WARN: "تحذير", UNKNOWN: "غير معروف"},
}


@dataclass(frozen=True)
class Check:
    title: str
    status: str
    detail: str
    advice: str = ""


class _Unknown(Exception):
    """A probe could not read its value. The message is a short, safe reason."""


def _reason(exc: BaseException) -> str:
    if isinstance(exc, _Unknown):
        return str(exc)
    if isinstance(exc, ActionError):
        return winshell.sanitize_failure(str(exc), limit=120)
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError)):
        return "Windows gave an unexpected answer"
    return "the probe failed"


def _ps_json(script: str, what: str):
    """Run one read-only PowerShell probe through the shared launcher and parse its JSON."""
    try:
        proc = run_powershell(script, PROBE_TIMEOUT)
    except (ActionError, OSError) as exc:
        raise _Unknown(f"{what} could not be read ({_reason(exc)})") from None
    if proc.returncode != 0:
        detail = winshell.sanitize_failure(proc.stderr, limit=120)
        raise _Unknown(f"{what} could not be read ({detail})")
    text = winshell.decode_output(proc.stdout)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                break
    raise _Unknown(f"{what} gave no readable answer")


# --- probes (PowerShell is read-only: Get-* and Test-Path only) ------------------------

_OS_SCRIPT = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "[pscustomobject]@{ totalKB = [int64]$os.TotalVisibleMemorySize; "
    "freeKB = [int64]$os.FreePhysicalMemory; "
    "bootUnix = [int64]([DateTimeOffset]$os.LastBootUpTime).ToUnixTimeSeconds() } | ConvertTo-Json -Compress"
)
_REBOOT_SCRIPT = (
    "$pending = $false; "
    "foreach ($k in @('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired', "
    "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending')) "
    "{ if (Test-Path -LiteralPath $k) { $pending = $true } }; "
    "$s = Get-ItemProperty -LiteralPath 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager' "
    "-Name PendingFileRenameOperations -ErrorAction SilentlyContinue; "
    "if ($s -and $s.PendingFileRenameOperations) { $pending = $true }; "
    "[pscustomobject]@{ pending = [bool]$pending } | ConvertTo-Json -Compress"
)
_DEFENDER_SCRIPT = (
    "$m = Get-MpComputerStatus; "
    "[pscustomobject]@{ rtp = [bool]$m.RealTimeProtectionEnabled; "
    "sigAgeDays = [int]$m.AntivirusSignatureAge } | ConvertTo-Json -Compress"
)
_FIREWALL_SCRIPT = (
    "Get-NetFirewallProfile | ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; "
    "enabled = [bool]$_.Enabled } } | ConvertTo-Json -Compress"
)
_UPDATE_SCRIPT = (
    "$s = Get-Service -Name wuauserv -ErrorAction Stop; "
    "[pscustomobject]@{ status = [string]$s.Status; startType = [string]$s.StartType } | ConvertTo-Json -Compress"
)


def _fixed_drive_letters() -> list[str]:
    """Letters of fixed local drives only (no floppy, optical, network or removable)."""
    if os.name != "nt":
        raise _Unknown("drive list is available on Windows only")
    import ctypes  # Windows only; imported lazily

    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    letters = []
    for index in range(2, 26):  # skip A and B
        if mask & (1 << index):
            letter = chr(ord("A") + index)
            if kernel32.GetDriveTypeW(f"{letter}:\\") == 3:  # DRIVE_FIXED
                letters.append(letter)
    return letters


def _gib(value: float) -> str:
    return f"{value / GIB:.1f} GB"


def _disk_checks(drives: Callable[[], list[str]], disk_usage: Callable) -> list[Check]:
    try:
        letters = drives()
    except Exception as exc:  # any failure becomes one unknown line, never a crash
        return [Check("Disk", UNKNOWN, f"unknown: {_reason(exc)}")]
    if not letters:
        return [Check("Disk", UNKNOWN, "unknown: no fixed drives were found")]
    checks = []
    for letter in letters:
        title = f"Disk {letter}"
        try:
            usage = disk_usage(f"{letter}:\\")
            free, total = float(usage.free), float(usage.total)
            if total <= 0:
                raise _Unknown("drive reports no size")
        except Exception as exc:
            checks.append(Check(title, UNKNOWN, f"unknown: {_reason(exc)}"))
            continue
        detail = f"{_gib(free)} free of {_gib(total)}"
        if free / total < LOW_DISK_FRACTION:
            checks.append(Check(title, WARN, detail,
                                f"Free up space on {letter}: (empty the Recycle Bin, uninstall unused apps)."))
        else:
            checks.append(Check(title, OK, detail))
    return checks


def _os_checks(powershell: Callable, now: datetime) -> list[Check]:
    try:
        data = powershell(_OS_SCRIPT, "memory and boot time")
        total, free = float(data["totalKB"]) * 1024, float(data["freeKB"]) * 1024
        if total <= 0:
            raise _Unknown("memory size was not reported")
        memory = Check("Memory", OK, f"{_gib(free)} free of {_gib(total)}")
        if free / total < LOW_MEMORY_FRACTION:
            memory = Check("Memory", WARN, f"{_gib(free)} free of {_gib(total)}",
                           "Close unused programs or restart Windows to free memory.")
    except Exception as exc:
        memory = Check("Memory", UNKNOWN, f"unknown: {_reason(exc)}")
    try:
        data = powershell(_OS_SCRIPT, "boot time")
        booted = datetime.fromtimestamp(int(data["bootUnix"]))  # local time; no locale-dependent parsing
        days = max((now - booted).total_seconds(), 0) / 86400
        boot_text = booted.strftime("%Y-%m-%d %H:%M")
        uptime = Check("Uptime", OK, f"{days:.1f} days")
        if days > LONG_UPTIME_DAYS:
            uptime = Check("Uptime", WARN, f"{days:.1f} days",
                           "Restart Windows to apply pending updates and clear stuck processes.")
        last_boot = Check("Last boot", OK, boot_text)
    except Exception as exc:
        uptime = Check("Uptime", UNKNOWN, f"unknown: {_reason(exc)}")
        last_boot = Check("Last boot", UNKNOWN, f"unknown: {_reason(exc)}")
    return [memory, uptime, last_boot]


def _reboot_check(powershell: Callable) -> Check:
    try:
        data = powershell(_REBOOT_SCRIPT, "restart status")
        if bool(data["pending"]):
            return Check("Restart pending", WARN, "yes",
                         "Restart Windows to finish installing updates.")
        return Check("Restart pending", OK, "no")
    except Exception as exc:
        return Check("Restart pending", UNKNOWN, f"unknown: {_reason(exc)}")


def _defender_check(powershell: Callable) -> Check:
    try:
        data = powershell(_DEFENDER_SCRIPT, "Windows Defender status")
        rtp, age = bool(data["rtp"]), int(data["sigAgeDays"])
    except Exception as exc:
        return Check("Windows Defender", UNKNOWN, f"unknown: {_reason(exc)}")
    detail = f"real-time protection {'on' if rtp else 'OFF'}, signatures {age} day(s) old"
    if not rtp and age > OLD_SIGNATURE_DAYS:
        return Check("Windows Defender", WARN, detail,
                     "Turn on real-time protection and update signatures in Windows Security.")
    if not rtp:
        return Check("Windows Defender", WARN, detail,
                     "Turn on real-time protection in Windows Security.")
    if age > OLD_SIGNATURE_DAYS:
        return Check("Windows Defender", WARN, detail,
                     "Update Defender signatures in Windows Security.")
    return Check("Windows Defender", OK, detail)


def _firewall_check(powershell: Callable) -> Check:
    try:
        data = powershell(_FIREWALL_SCRIPT, "firewall profiles")
        profiles = data if isinstance(data, list) else [data]
        states = [(str(p["name"]), bool(p["enabled"])) for p in profiles]
    except Exception as exc:
        return Check("Firewall", UNKNOWN, f"unknown: {_reason(exc)}")
    if not states:
        return Check("Firewall", UNKNOWN, "unknown: no firewall profiles were reported")
    off = [name for name, enabled in states if not enabled]
    if off:
        return Check("Firewall", WARN, "off for " + ", ".join(off),
                     "Turn the firewall on for the " + ", ".join(off) + " profile (Windows Security).")
    return Check("Firewall", OK, "on for all profiles")


def _update_check(powershell: Callable) -> Check:
    try:
        data = powershell(_UPDATE_SCRIPT, "Windows Update service")
        start = str(data["startType"])
        status = str(data["status"])
    except Exception as exc:
        return Check("Windows Update service", UNKNOWN, f"unknown: {_reason(exc)}")
    detail = f"{status}, start type {start}"
    if start.lower() == "disabled":
        return Check("Windows Update service", WARN, detail,
                     "Set the Windows Update service to Manual or Automatic (services.msc).")
    return Check("Windows Update service", OK, detail)


def run_health(*, windows: bool | None = None,
               powershell: Callable | None = None,
               drives: Callable[[], list[str]] | None = None,
               disk_usage: Callable | None = None,
               now: datetime | None = None) -> list[Check]:
    """Probe every item. Injected callables exist for tests; defaults use the real PC."""
    on_windows = (os.name == "nt") if windows is None else windows
    if not on_windows:
        reason = "unknown: this check runs on Windows only"
        return [Check(title, UNKNOWN, reason) for title in
                ("Disk", "Memory", "Uptime", "Last boot", "Restart pending",
                 "Windows Defender", "Firewall", "Windows Update service")]
    if powershell is None:
        powershell = _ps_json
    if drives is None:
        drives = _fixed_drive_letters
    if disk_usage is None:
        import shutil
        disk_usage = shutil.disk_usage
    now = now or datetime.now()
    checks: list[Check] = []
    checks += _disk_checks(drives, disk_usage)
    checks += _os_checks(powershell, now)
    checks.append(_reboot_check(powershell))
    checks.append(_defender_check(powershell))
    checks.append(_firewall_check(powershell))
    checks.append(_update_check(powershell))
    return checks


def render(checks: Iterable[Check], language: str = "en") -> str:
    """The Telegram message (HTML). Only the fixed text and numbers built above appear."""
    words = VERDICT.get(language, VERDICT["en"])
    lines = ["🩺 <b>PC health check</b> (read-only, nothing was changed)"]
    warnings = unknown = 0
    for check in checks:
        lines.append(f"{MARKERS[check.status]} {words[check.status]} · "
                     f"{html.escape(check.title)}: {html.escape(check.detail)}")
        if check.status == WARN:
            warnings += 1
            if check.advice:
                lines.append(f"    ↳ {html.escape(check.advice)}")
        elif check.status == UNKNOWN:
            unknown += 1
    lines.append("")
    lines.append(f"{warnings} warning(s), {unknown} unknown.")
    return "\n".join(lines)
