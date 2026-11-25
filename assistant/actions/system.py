"""System-level actions: info, processes, volume, clipboard, power, toasts.

Every one of these is read-only or clearly reversible except ``kill_process``
and ``power``, which are marked high risk in :mod:`assistant.action_spec`.
Nothing here is hidden, persistent or self-installing.
"""

from __future__ import annotations

import os
import base64
import io
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

from ..winshell import sanitize_failure

from ..config import Config
from .base import (
    IS_WINDOWS,
    ActionContext,
    ActionError,
    ActionResult,
    human_bytes,
    human_duration,
    ps_literal,
    run_powershell,
    run_process,
    truncate,
    windows_only,
)
from .core import encode_photo


# ---------------------------------------------------------------------------
# system_info
# ---------------------------------------------------------------------------

SYSTEM_INFO_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$r = [ordered]@{}
$r.Host = $env:COMPUTERNAME
try {
  $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
  if ($cpu) {
    $r.CPU = ('{0} ({1} cores / {2} threads) - load {3}%' -f `
      $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors, $cpu.LoadPercentage)
  }
} catch {}
try {
  $os = Get-CimInstance Win32_OperatingSystem
  $total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
  $free = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
  $used = [math]::Round($total - $free, 1)
  $r.Memory = ('{0} GB used of {1} GB ({2} GB free)' -f $used, $total, $free)
  $up = (Get-Date) - $os.LastBootUpTime
  $r.Uptime = ('{0}d {1}h {2}m' -f $up.Days, $up.Hours, $up.Minutes)
} catch {}
try {
  $disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    '{0} {1} free of {2}' -f $_.DeviceID,
      [math]::Round($_.FreeSpace / 1GB, 1), [math]::Round($_.Size / 1GB, 1)
  }
  if ($disks) { $r.Disks = ($disks -join '; ') }
} catch {}
try {
  $bat = Get-CimInstance Win32_Battery | Select-Object -First 1
  if ($bat) {
    $state = switch ($bat.BatteryStatus) {
      2 { 'on battery' } 1 { 'discharging' } 3 { 'charging' } default { "status $($bat.BatteryStatus)" }
    }
    $r.Battery = ('{0}% ({1})' -f $bat.EstimatedChargeRemaining, $state)
  }
} catch {}
try {
  $ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' } |
    ForEach-Object { '{0} ({1})' -f $_.IPAddress, $_.InterfaceAlias }
  if ($ips) { $r.IPv4 = ($ips -join '; ') }
} catch {}
try {
  $apps = Get-Process | Where-Object { $_.MainWindowTitle } |
    Select-Object -First 12 |
    ForEach-Object { '{0} - {1}' -f $_.ProcessName, $_.MainWindowTitle }
  if ($apps) { $r.Windows = ($apps -join '; ') }
} catch {}
foreach ($k in $r.Keys) { '{0}={1}' -f $k, $r[$k] }
"""

INFO_ORDER = ["CPU", "Memory", "Disks", "Battery", "Uptime", "IPv4", "Windows", "Host"]
INFO_ICONS = {
    "CPU": "🧠", "Memory": "💾", "Disks": "🗄️", "Battery": "🔋",
    "Uptime": "⏱️", "IPv4": "🌐", "Windows": "🪟", "Host": "💻",
}


def render_system_info(report: dict[str, str], sections: list[str] | None = None) -> str:
    """Turn a report dict into the message text (pure, easy to test)."""
    wanted = {s.strip().lower() for s in (sections or []) if s.strip()}
    lines = ["📊 System information", ""]
    shown = 0
    for key in INFO_ORDER:
        if key not in report:
            continue
        if wanted and key.lower() not in wanted:
            continue
        lines.append(f"{INFO_ICONS.get(key, '•')} {'':<0}{key}")
        for chunk in str(report[key]).split("; "):
            lines.append(f"    {chunk.strip()}")
        shown += 1
    if not shown:
        return "📊 System information\n\n(no information available on this platform)"
    return "\n".join(lines)


def _portable_report(config: Config) -> dict[str, str]:
    """Best-effort report on non-Windows (also used by the test-suite)."""
    report: dict[str, str] = {}

    cpu = platform.processor() or platform.machine() or "unknown CPU"
    try:
        load = os.getloadavg()[0]
        cpu = f"{cpu} - load {load:.2f}"
    except (AttributeError, OSError):
        pass
    report["CPU"] = cpu

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            values = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                values[key] = float(rest.strip().split(" ")[0]) * 1024
            total = values.get("MemTotal", 0)
            free = values.get("MemAvailable", values.get("MemFree", 0))
            report["Memory"] = (
                f"{human_bytes(total - free)} used of {human_bytes(total)} "
                f"({human_bytes(free)} free)"
            )
        except Exception:
            pass

    try:
        usage = shutil.disk_usage("/")
        report["Disks"] = f"/ {human_bytes(usage.free)} free of {human_bytes(usage.total)}"
    except OSError:
        pass

    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        report["Uptime"] = human_duration(uptime)
    except Exception:
        pass

    try:
        host = socket.gethostname()
        report["Host"] = host
        report["IPv4"] = socket.gethostbyname(host)
    except Exception:
        pass
    return report


def system_info(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    sections = [s for s in (params.get("sections") or "").split(",") if s.strip()]

    if IS_WINDOWS:
        proc = run_powershell(SYSTEM_INFO_PS, min(config.command_timeout, 45), config.workspace_root)
        report: dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            key, _, value = line.partition("=")
            if key.strip() and value.strip():
                report[key.strip()] = value.strip()
        if not report:
            raise ActionError(
                "Could not read system information. PowerShell said: "
                f"{sanitize_failure(proc.stderr, 200)}"
            )
        backend = "Windows / PowerShell"
    else:
        report = _portable_report(config)
        backend = "portable (non-Windows)"

    text = render_system_info(report, sections)
    return ActionResult(
        "system_info", True,
        truncate(f"{text}\n\n<i>source: {backend}</i>", config.max_output_chars),
        duration=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------

#: Processes the assistant refuses to kill (killing them can crash or log off).
PROTECTED_PROCESSES = frozenset({
    "system", "system idle process", "idle", "registry", "memory compression",
    "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost",
    "dwm", "fontdrvhost", "sihost", "ctfmon", "logonui", "winlogon",
    "spoolsv", "audiodg", "msmpeng", "nissrv", "securityhealthservice",
    "smartscreen", "taskhostw", "runtimebroker", "startmenuexperiencehost",
    "shellexperiencehost", "searchhost", "textinputhost", "dllhost",
    "wudfhost", "wininit", "sppsvc", "trustedinstaller", "wmiprvse",
})

SORT_MODES = {"cpu", "memory", "name"}


def _process_name_key(name: str) -> str:
    return name.strip().lower().removesuffix(".exe")


def is_protected_process(name: str, config: Config) -> bool:
    key = _process_name_key(name)
    if key in PROTECTED_PROCESSES:
        return True
    extra = {_process_name_key(p) for p in config.protected_processes}
    return key in extra


def list_processes(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    sort = (params.get("sort") or "cpu").strip().lower()
    if sort not in SORT_MODES:
        raise ActionError(f"'sort' must be one of {sorted(SORT_MODES)} (got {sort!r}).")
    try:
        limit = int(params.get("limit") or 25)
    except ValueError:
        raise ActionError("'limit' must be a number.") from None
    limit = max(1, min(limit, 100))

    if IS_WINDOWS:
        sort_clause = {
            "cpu": "Sort-Object CPU -Descending",
            "memory": "Sort-Object WorkingSet64 -Descending",
            "name": "Sort-Object ProcessName",
        }[sort]
        command = (
            "Get-Process | " + sort_clause + f" | Select-Object -First {limit} "
            "| ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessName, $_.Id, "
            "[math]::Round($_.CPU,1), [math]::Round($_.WorkingSet64/1MB,1) }"
        )
        proc = run_powershell(command, min(config.command_timeout, 45), config.workspace_root)
        rows = [line for line in (proc.stdout or "").splitlines() if "|" in line]
        header = f"{'NAME':<28}{'PID':>8}{'CPU(s)':>9}{'RAM(MB)':>10}"
        body = [header, "-" * len(header)]
        for row in rows[:limit]:
            name, pid, cpu, ram = (row.split("|") + ["", "", "", ""])[:4]
            body.append(f"{name[:27]:<28}{pid:>8}{cpu:>9}{ram:>10}")
    else:
        flag = {"cpu": "pcpu", "memory": "rss", "name": "comm"}[sort]
        proc = run_process(
            ["ps", "-eo", "pid=,pcpu=,rss=,comm=", f"--sort=-{flag}" if sort != "name" else "--sort=comm"],
            min(config.command_timeout, 30),
        )
        header = f"{'NAME':<28}{'PID':>8}{'CPU(s)':>9}{'RAM(MB)':>10}"
        body = [header, "-" * len(header)]
        for row in (proc.stdout or "").splitlines()[:limit]:
            bits = row.split()
            if len(bits) < 4:
                continue
            pid, cpu, rss, name = bits[0], bits[1], bits[2], " ".join(bits[3:])
            ram_mb = float(rss) / 1024 if rss.isdigit() else 0.0
            body.append(f"{name[:27]:<28}{pid:>8}{cpu:>9}{ram_mb:>10.1f}")

    if len(body) <= 2:
        raise ActionError("Could not read the process list on this platform.")
    return ActionResult(
        "list_processes", True,
        truncate(f"📋 Processes (sorted by {sort})\n\n<pre>" + "\n".join(body) + "</pre>",
                 config.max_output_chars),
        duration=time.perf_counter() - started,
    )


def kill_process(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()
    name = (params.get("name") or "").strip()
    pid_raw = (params.get("pid") or "").strip()
    force = (params.get("force") or "true").strip().lower() in {"1", "true", "yes", "on"}

    if not name and not pid_raw:
        raise ActionError("Give either a process name or a PID.")
    if name and pid_raw:
        raise ActionError("Give only one of name or pid.")

    pid: int | None = None
    if pid_raw:
        if not pid_raw.isdigit():
            raise ActionError(f"'pid' must be a number (got {pid_raw!r}).")
        pid = int(pid_raw)
        if pid <= 4:
            raise ActionError(f"Refused: PID {pid} is a protected system process.")
        if pid in {os.getpid(), os.getppid()}:
            raise ActionError("Refused: that is the assistant's own process.")
        # A PID must not bypass the protected-name list. Fail closed if its
        # identity cannot be resolved; PID reuse still makes this best effort.
        if IS_WINDOWS:
            lookup = run_powershell(
                f"$p = Get-Process -Id {pid} -ErrorAction Stop; $p.ProcessName",
                min(config.command_timeout, 30), config.workspace_root,
            )
            process_name = (lookup.stdout or "").strip()
            if lookup.returncode != 0 or not process_name:
                raise ActionError("Refused: could not resolve PID to a process name.")
        else:
            try:
                process_name = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                raise ActionError("Refused: could not resolve PID to a process name.") from None
        if is_protected_process(process_name, config):
            raise ActionError(f"Refused: PID {pid} is protected ({process_name}).")

    if name:
        if is_protected_process(name, config):
            raise ActionError(
                f"Refused: '{name}' is on the protected list of critical Windows "
                "processes. Add to [behaviour] protected_processes only if you are "
                "sure, or kill it yourself."
            )

    if IS_WINDOWS:
        if pid is not None:
            command = f"Stop-Process -Id {pid}" + (" -Force" if force else "")
        else:
            command = f"Stop-Process -Name {ps_literal(name)}" + (" -Force" if force else "")
        proc = run_powershell(command, min(config.command_timeout, 30), config.workspace_root)
        if proc.returncode != 0:
            raise ActionError(f"Could not kill: {sanitize_failure(proc.stderr, 300)}")
        target = f"PID {pid}" if pid is not None else name
        return ActionResult("kill_process", True, f"Stopped {target}.",
                            duration=time.perf_counter() - started)

    # POSIX (used by the test-suite)
    if pid is not None:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            raise ActionError(f"No process with PID {pid}.") from None
        except PermissionError:
            raise ActionError(f"Not allowed to kill PID {pid}.") from None
        return ActionResult("kill_process", True, f"Sent SIGTERM to PID {pid}.",
                            duration=time.perf_counter() - started)

    proc = run_process(["pkill", "-15", "-x", name.removesuffix(".exe")], 15)
    if proc.returncode != 0:
        raise ActionError(f"No process named '{name}' was found.")
    return ActionResult("kill_process", True, f"Sent SIGTERM to processes named {name}.",
                        duration=time.perf_counter() - started)


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

VOLUME_MODES = {"toggle", "mute", "unmute", "up", "down", "set"}

#: One volume key press changes the level by ~2 % on Windows.
VOLUME_STEP_PERCENT = 2


def volume_key_sequence(mode: str, level: int | None, steps: int) -> list[str]:
    """Which media keys to press (pure function, unit-tested)."""
    if mode in {"mute", "unmute", "toggle"}:
        return ["mute"]
    if mode == "up":
        return ["up"] * steps
    if mode == "down":
        return ["down"] * steps
    if mode == "set":
        assert level is not None
        # Guarantee the floor, then climb to the requested level.
        presses_up = max(0, min(50, round(level / VOLUME_STEP_PERCENT)))
        return ["down"] * 55 + ["up"] * presses_up
    raise ActionError(f"Unknown volume mode {mode!r}.")


VOLUME_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$sig = @'
[DllImport("user32.dll")]
public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, System.UIntPtr dwExtraInfo);
'@
$k = Add-Type -MemberDefinition $sig -Name 'VolKeys' -Namespace 'TpaVol' -PassThru
$codes = @({codes})
foreach ($c in $codes) {
  $k::keybd_event([byte]$c, 0, 0, [System.UIntPtr]::Zero)
  $k::keybd_event([byte]$c, 0, 2, [System.UIntPtr]::Zero)
  Start-Sleep -Milliseconds 15
}
'pressed ' + $codes.Count + ' volume key(s)'
"""


def volume(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    config = ctx.config
    started = time.perf_counter()

    # No silent fallback: only an explicitly requested mode may touch the
    # volume. A numeric "level" implies mode "set"; without either, nothing
    # happens and the step fails with a clear error.
    mode = (params.get("mode") or "").strip().lower()

    level: int | None = None
    if "level" in params and params["level"].strip():
        if not params["level"].strip().isdigit():
            raise ActionError("'level' must be a whole number between 0 and 100.")
        level = int(params["level"].strip())
        if not 0 <= level <= 100:
            raise ActionError("'level' must be between 0 and 100.")
        mode = "set"

    if not mode:
        raise ActionError(
            "'mode' is required for volume — one of "
            + ", ".join(sorted(VOLUME_MODES)) + " (or a numeric 'level'). "
            "Nothing was changed."
        )
    if mode not in VOLUME_MODES:
        raise ActionError(
            f"'mode' must be one of {sorted(VOLUME_MODES)} (got {mode!r}). "
            "Nothing was changed."
        )
    if mode == "set" and level is None:
        raise ActionError(
            "Mode 'set' needs a 'level' between 0 and 100. Nothing was changed."
        )

    try:
        steps = int(params.get("steps") or 5)
    except ValueError:
        raise ActionError("'steps' must be a number.") from None
    steps = max(1, min(steps, 50))

    sequence = volume_key_sequence(mode, level, steps)
    codes = {
        "mute": VK_VOLUME_MUTE, "down": VK_VOLUME_DOWN, "up": VK_VOLUME_UP,
    }
    code_list = ",".join(str(codes[item]) for item in sequence)

    if not IS_WINDOWS:
        raise windows_only("Changing the volume")

    script = VOLUME_PS_TEMPLATE.replace("{codes}", code_list)
    proc = run_powershell(script, min(config.command_timeout, 30), config.workspace_root)
    if proc.returncode != 0:
        raise ActionError(f"Could not change the volume: {sanitize_failure(proc.stderr, 300)}")

    if mode == "set":
        detail = (f"Volume set to ~{level}% (≈{VOLUME_STEP_PERCENT}% per key press, "
                  f"{len(sequence)} presses).")
    elif mode == "toggle":
        detail = "Mute toggled (press again to undo)."
    elif mode in {"mute", "unmute"}:
        detail = f"Mute toggled — Windows has no read-back, so '{mode}' flips the current state."
    else:
        detail = f"Volume {mode}: {len(sequence)} key press(es)."
    return ActionResult("volume", True, detail, duration=time.perf_counter() - started)


# ---------------------------------------------------------------------------
# lock screen / clipboard / notifications
# ---------------------------------------------------------------------------

def lock_screen(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    if not IS_WINDOWS:
        raise windows_only("Locking the screen")
    proc = run_process(["rundll32.exe", "user32.dll,LockWorkStation"], 15)
    if proc.returncode != 0:
        raise ActionError(f"Could not lock the screen: {sanitize_failure(proc.stderr, 200)}")
    return ActionResult("lock_screen", True, "Screen locked.",
                        duration=time.perf_counter() - started)


def _clipboard_read() -> str:
    """Platform clipboard read (monkeypatched in tests)."""
    if IS_WINDOWS:
        proc = run_powershell("Get-Clipboard -Raw", 20)
        if proc.returncode != 0:
            raise ActionError(f"Could not read the clipboard: {sanitize_failure(proc.stderr, 200)}")
        return proc.stdout.rstrip("\r\n")
    for tool in (["xclip", "-selection", "clipboard", "-o"],
                 ["xsel", "--clipboard", "--output"],
                 ["pbpaste"]):
        if shutil.which(tool[0]):
            proc = run_process(tool, 15)
            if proc.returncode == 0:
                return proc.stdout
    raise ActionError("No clipboard tool available (xclip/xsel/pbpaste).")


def _clipboard_write(text: str) -> None:
    """Platform clipboard write (monkeypatched in tests)."""
    if IS_WINDOWS:
        # Passed as base64 data, never pasted into the script (see ps_literal).
        proc = run_powershell(f"Set-Clipboard -Value {ps_literal(text)}", 20)
        if proc.returncode != 0:
            raise ActionError(f"Could not write the clipboard: {sanitize_failure(proc.stderr, 200)}")
        return
    for tool in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                 ["pbcopy"]):
        if shutil.which(tool[0]):
            try:
                proc = subprocess.run(
                    tool, input=text, text=True, encoding="utf-8",
                    capture_output=True, timeout=15,
                )
            except OSError:
                continue
            if proc.returncode == 0:
                return
    raise ActionError("No clipboard tool available (xclip/xsel/pbcopy).")


def clipboard_get(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    text = _clipboard_read()
    if not text:
        detail = "(the clipboard is empty)"
    else:
        detail = truncate(text, min(ctx.config.max_output_chars, 3000))
    return ActionResult("clipboard_get", True, f"📋 Clipboard content:\n\n{detail}",
                        duration=time.perf_counter() - started)


def clipboard_set(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    text = params.get("text") or ""
    if not text.strip():
        raise ActionError("Nothing to put in the clipboard.")
    _clipboard_write(text)
    return ActionResult("clipboard_set", True,
                        f"Copied {len(text)} characters to the clipboard.",
                        duration=time.perf_counter() - started)


NOTIFY_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title}'))
$n.BalloonTipText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{text}'))
$n.Visible = $true
$n.ShowBalloonTip(6000)
Start-Sleep -Milliseconds 1200
$n.Dispose()
'notification shown'
"""


def notification(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    text = (params.get("text") or "").strip()
    title = (params.get("title") or "PC Assistant").strip()
    if not text:
        raise ActionError("Nothing to show in the notification.")

    if IS_WINDOWS:
        script = NOTIFY_PS.replace("{title}", base64.b64encode(title.encode("utf-8")).decode("ascii")).replace("{text}", base64.b64encode(text.encode("utf-8")).decode("ascii"))
        proc = run_powershell(script, min(ctx.config.command_timeout, 30),
                              ctx.config.workspace_root)
        if proc.returncode != 0:
            raise ActionError(f"Could not show the notification: {sanitize_failure(proc.stderr, 300)}")
    else:
        if not shutil.which("notify-send"):
            raise ActionError("Desktop notifications need Windows or notify-send.")
        proc = run_process(["notify-send", title, text], 15)
        if proc.returncode != 0:
            raise ActionError("notify-send failed.")

    return ActionResult("notification", True, f"Showed notification: {title} — {text}",
                        duration=time.perf_counter() - started)


# ---------------------------------------------------------------------------
# window screenshot
# ---------------------------------------------------------------------------

WINDOW_SHOT_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$sig = @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
public struct RECT { public int Left, Top, Right, Bottom; }
'@
$w = Add-Type -MemberDefinition $sig -Name 'WinApi' -Namespace 'TpaWin' -PassThru
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title}'))
$handle = [IntPtr]::Zero
if ($title.Trim().Length -gt 0) {
  $p = Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowTitle.IndexOf($title, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 } |
       Select-Object -First 1
  if (-not $p) { Write-Error "No window matching '$title'"; exit 2 }
  $handle = $p.MainWindowHandle
} else {
  $handle = [IntPtr]::new([Int64]'{handle}')
  if ($handle -eq [IntPtr]::Zero) { Write-Error 'No active window available'; exit 4 }
}
$rect = New-Object TpaWin.WinApi+RECT
$null = $w::GetWindowRect($handle, [ref]$rect)
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -le 0 -or $height -le 0 -or $width -gt 12000 -or $height -gt 12000) { Write-Error 'Could not measure the window'; exit 3 }
$bmp = New-Object System.Drawing.Bitmap $width, $height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
try { $ok = $w::PrintWindow($handle, $hdc, 2) } finally { $g.ReleaseHdc($hdc) }
if (-not $ok) { Write-Error 'This window cannot be captured'; exit 5 }
$mem = New-Object System.IO.MemoryStream
$bmp.Save($mem, [System.Drawing.Imaging.ImageFormat]::Png)
[Convert]::ToBase64String($mem.ToArray())
$mem.Dispose(); $g.Dispose(); $bmp.Dispose()
"""


def window_screenshot(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    if not ctx.capture_allowed:
        raise ActionError("Window capture requires approval and a visible sharing indicator.")
    if params.get("path"):
        raise ActionError("Saving screenshots to disk is disabled. Request a photo instead.")
    if not IS_WINDOWS:
        raise windows_only("Window screenshots")
    started = time.perf_counter()
    title = (params.get("title") or "").strip()
    title64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
    proc = run_powershell(WINDOW_SHOT_PS.replace("{title}", title64).replace("{handle}", str(ctx.capture_window_handle)),
                          min(ctx.config.command_timeout, 45), ctx.config.workspace_root)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        # Never fall back to the whole screen: that could expose extra data.
        raise ActionError("Window capture failed or no matching window was found; no full-screen fallback was taken.")
    try:
        encoded = ''.join(proc.stdout.split())
        if len(encoded) > 30_000_000:
            raise ValueError("image too large")
        png = base64.b64decode(encoded, validate=True)
        from PIL import Image
        with Image.open(io.BytesIO(png)) as image:
            photo = encode_photo(image)
    except Exception as exc:
        raise ActionError("Could not decode the window capture.") from exc
    return ActionResult("window_screenshot", True,
                        "Window photo captured in memory" + (f" (title: {title})" if title else " (active window)"),
                        photo=photo, duration=time.perf_counter() - started)


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------

POWER_MODES = {"shutdown", "restart", "sleep", "hibernate", "cancel"}
MIN_POWER_DELAY = 30


def power_argv(mode: str, delay: int) -> list[str]:
    """Command line for a power action (pure function, unit-tested)."""
    mode = mode.strip().lower()
    if mode == "shutdown":
        return ["shutdown", "/s", "/t", str(delay)]
    if mode == "restart":
        return ["shutdown", "/r", "/t", str(delay)]
    if mode == "cancel":
        return ["shutdown", "/a"]
    if mode == "sleep":
        return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    if mode == "hibernate":
        return ["shutdown", "/h"]
    raise ActionError(f"Unknown power mode {mode!r}.")


def power(params: dict[str, str], ctx: ActionContext) -> ActionResult:
    started = time.perf_counter()
    mode = (params.get("mode") or "").strip().lower()
    if mode not in POWER_MODES:
        raise ActionError(f"'mode' must be one of {sorted(POWER_MODES)} (got {mode!r}).")

    try:
        delay = int(params.get("delay") or MIN_POWER_DELAY)
    except ValueError:
        raise ActionError("'delay' must be a number of seconds.") from None

    if mode in {"shutdown", "restart"}:
        delay = max(MIN_POWER_DELAY, min(delay, 3600))
    elif mode in {"sleep", "hibernate"}:
        delay = 0
    else:
        delay = 0

    if not IS_WINDOWS:
        raise windows_only(f"Power action '{mode}'")

    argv = power_argv(mode, delay)
    proc = run_process(argv, 30)
    if proc.returncode != 0:
        raise ActionError(
            f"Power action failed (exit {proc.returncode}): "
            f"{sanitize_failure(proc.stderr or proc.stdout, 200)}"
        )

    if mode in {"shutdown", "restart"}:
        message = (f"{mode.capitalize()} scheduled in {delay}s. "
                   f"Cancel it with the 'power' action, mode 'cancel'.")
    elif mode == "cancel":
        message = "Pending shutdown/restart cancelled."
    else:
        message = f"{mode.capitalize()} requested."
    return ActionResult("power", True, message, duration=time.perf_counter() - started)
