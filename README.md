# Telegram PC Assistant 3.1

A Windows 10/11 program that turns Arabic or English Telegram requests into reviewable PC actions. It runs as **your Windows user**, uses Telegram polling and a cloud, local Ollama, or simple keyword planner. Review every plan before approving it; the program is not a security sandbox.

## Quick setup

Prerequisites: Windows 10/11, Python 3.11+ installed with **Add Python to PATH** (Pillow 13 requires 3.11), and a Telegram bot token from **@BotFather** (`/newbot`).

1. **Unzip** the project into a folder on your PC.
2. Put a UTF-8 plain-text file named **`token.txt`** in that folder, next to `install.bat`. Paste the bot token on its own line. The program uses the first non-empty line, removing surrounding whitespace and a UTF-8 BOM. Do not use `token.txt.txt`.
3. Double-click **`install.bat`**. It creates the environment, installs dependencies, checks the setup, and starts the assistant when `token.txt` is present. Keep the window open and the PC awake.
4. **Message your bot on Telegram**: open its private chat, press Start, and send `/help`.

**Important:** the shipped `allowed_user_ids` is `7823157087`. If that is not your numeric Telegram user ID, edit it in `config.ini` (find yours via @userinfobot), then restart using `start_assistant.bat`. The bot ignores other requesters. Internet is needed for installation and Telegram.

### Token options and security

`token.txt` is **plain text**, not encrypted: anyone who can read it can control the bot. Restrict file access with Windows permissions; never share or commit it. It is not included in the release ZIP and `.gitignore` excludes it. The program leaves it in place and does not print or log the token; delete the file yourself when no longer needed. If it leaks, revoke the token with BotFather and replace it.

Priority is **`token.txt` → `TELEGRAM_BOT_TOKEN` → per-user stored token → hidden first-run prompt**. Empty or whitespace-only `token.txt` falls through; an unreadable file or invalid non-empty first line causes an error instead of silently using a lower-priority token. The program checks only the project-root file, even when `--config` points elsewhere. Legacy `bot_token` in `config.ini` is ignored; move it to one of the supported sources. `--set-token` saves to the store but does not override an existing `token.txt` or environment variable. `--forget-token` removes only the stored token, not `token.txt` or the environment variable.

Without `token.txt`, the installer offers the hidden-input per-user store, and normal startup prompts if no source is available. On Windows the store is protected with DPAPI when available under `%APPDATA%\TelegramPCAssistant\token.json`; if DPAPI is unavailable the fallback is plaintext with restricted permissions. `install.bat` never asks for the token if `token.txt` exists, even if it is empty; in that case fix it or start the bot manually to use the prompt fallback.

There is no hidden or automatic startup launcher and no built-in keylogging. Keep the normal assistant window open. An interactive Windows desktop session is needed for screenshots and GUI operations. Optional local planner: install <https://ollama.com/download>, run `ollama pull qwen2.5:3b`, and set `[planner] mode = local`. `mode = keywords` uses offline rules and may understand fewer requests.

## Privacy first

* With the default `mode = cloud`, **a separate confirmation is required before** sending your request and system context (local date/time, OS, home and working folder) to the configured third-party planner, by default `text.pollinations.ai`. A second plan requires its own confirmation before PC actions. Only literal loopback IPs (`127.0.0.1`, `::1`) are treated as local; other Ollama hosts, including `localhost`, require approval. Planner requests do not use HTTP proxy settings or follow redirects, so a redirected or proxy-only endpoint may fail. Service availability and free-tier limits are not guaranteed. Fallback after approval is cloud → configured Ollama → keyword rules.
* `mode = local` with a loopback Ollama endpoint, or `keywords`, keeps planning local, but **Telegram still carries messages, voice notes, plans, results and any transferred files**. Telegram bot chats are not secret chats. The bot ignores groups and only accepts private chats from allowed user IDs. Configured approvers see plan parameters too.
* Voice transcription runs locally using an explicitly installed model. It never sends voice transcripts to the cloud planner: voice requests use the offline keyword planner. The model's initial download and package installation require internet.
* The new audit log records **metadata only**, not request text, notes, commands, action output or exception traces. Existing logs from earlier versions may still contain sensitive material—delete or protect old logs. Personal notes and macros are unencrypted local JSON; do not save passwords, tokens or other secrets. They can also appear in Telegram plans/results when explicitly requested and approved.

## Approval and safety

1. Send a request such as `find PDF files in C:\Users\Me\Documents`, `show running processes`, or `remind me in 10 minutes to drink water`. Prefix a command with `!` to request raw PowerShell. Natural-language plans may be imperfect: **check actual action parameters**, not just the summary.
2. A proposed action plan shows each action, its exact parameters, a rough risk label, a six-character code and an eight-character fingerprint. If it is too large to show in a single confirmation message, it is rejected; split the request. Reply-only answers do not require confirmation.
3. Use ✅ / ❌ or send `approve CODE` / `deny CODE` (also `موافق CODE` / `رفض CODE`). `/yes CODE` and `/no CODE` also work. Codes expire after 15 minutes, are single-use, and are invalidated when the same requester replans in the same chat. The fingerprint is a short SHA-256 digest of the canonical steps, intended to detect accidental changes; it is not a signature or a substitute for review.
4. Steps execute in sequence after approval; the result is reported to the requesting chat. If a step fails, later steps may still run. Creating a reminder, daily briefing or scheduled command requires approval; **when due, each occurrence creates a new approval plan**. Nothing is delivered or executed until that plan is approved. Each plan expires after 15 minutes.

Risk labels are approximate, **not security boundaries**: even a "low" read, screenshot, URL or open action may expose sensitive information or launch software. `powershell`, `kill_process`, `power`, and `scheduled_task` are high risk. PowerShell can bypass file restrictions, launch arbitrary programs and modify or delete data with the permissions of the Windows account. `blocked_patterns`, file roots and protected-process lists are additional guardrails, not a sandbox. Do not run the bot as administrator unless necessary.

### Two-person approval

By default all allowed users are also approvers. All approvers **must also be listed in `allowed_user_ids`**; accounts outside that list cannot use the bot, approve plans or invoke `/panic`. For a different-person approval, for example:

```ini
[telegram]
allowed_user_ids = 111111111, 222222222
approver_user_ids = 222222222
require_distinct_approver = true
```

Both users can request actions, but only configured approvers can approve; with the distinct rule enabled, the requester cannot approve their own plan. The approver must first start the bot in their private chat. If user 222222222 requests a plan, another allowed approver must be configured or that request cannot be approved. This supersedes older configurations that listed an approver outside `allowed_user_ids`: add them to the allowed list or the config is rejected. Protect both Telegram accounts and devices.

`/panic` cancels pending plans, sets an abort flag, and attempts to kill tracked child processes; plans stop at their next check. It cannot undo completed actions, guarantee termination of process descendants, stop every GUI/system operation, reverse a shutdown already requested, or delete scheduled jobs. It pauses due jobs while the abort flag is set; approving a new plan clears the flag. Review `/jobs` and use `/canceltask CODE` to remove persisted jobs separately. Shutdown/restart have a minimum 30-second delay; request `power cancel` to attempt cancellation. Sleep and hibernate may be immediate.

## Security model and limits

This section lists what the code enforces and what it does not. The offline tests cover the enforcement logic. Windows-only parts (PowerShell, the clipboard, process control, DPAPI, capture, the banner's global hotkey, `cloudflared` runtime, and live Telegram) have **not** been run on Windows in the development sandbox; they were tested with fakes.

**Enforced in code**

* **Who can act.** Only the allowed Telegram user ID in a private chat can use the bot. Groups, channels, inline queries and edited messages are ignored. Voice notes, macros, scheduled jobs, reminders, briefings and text from forwarded messages cannot run anything without a plan you approve.
* **Approvals.** Every plan needs your Yes or `approve CODE`. A code is bound to the exact steps (fingerprint), works once, and expires after 15 minutes. A replan voids the earlier code. A repeated or concurrent press runs the plan at most once. A stale button, a plan the store no longer holds, or an expired plan runs nothing. At most 50 plans can wait for approval.
* **Planner output.** Local, Ollama and cloud output must match the schema and the action allowlist. The planner cannot create `remote_start`; the generic step executor refuses it too.
* **Paths.** UNC and network paths (`\\server\share`, `//server`, `\\?\`, `\\.\`), reserved device names in any part of a path (`CON`, `NUL`, `COM1`, `LPT1`…), and alternate data streams (`file.txt:stream`) are refused. Symlinks and junctions are resolved before the check, so a link that leaves the allowed roots is refused.
* **Received files.** Names are sanitized: no folders, no streams, and device names get a `_` prefix (`CON.exe` becomes `_CON.exe`). The file is written to a temporary name and renamed only after the size check. Uploads over `max_send_file_mb` are refused before download and deleted after it if they grew past the limit. The project does not extract archives, so there is no zip-bomb path.
* **Credentials.** File actions never read, send, list or overwrite `token.json`, `token.txt`, `.env` or the token folder, even inside an allowed root or via a link. Writes into the Windows Startup folder are refused. The bot token is never placed in `config.ini`, plans, Telegram messages or logs. Every log handler, including library warnings, replaces the token and any other registered secret with `[REDACTED]`. Exception text is not logged, and the remote link is never logged or stored.
* **Clipboard and process names.** Text sent to PowerShell (`clipboard_set`, `kill_process` by name, `open` fallback) travels as base64, so quotes, including Unicode quotes that PowerShell also treats as string delimiters, cannot change the script.
* **Denial of service.** Each user may send 30 updates per 10 seconds; more are dropped. Requests over 2000 characters are refused before any planner sees them. Strangers are logged at most once a minute, without their message text. Logs rotate at 2 MB with 5 backups.
* **Remote desktop.** Requests must carry a Host and, when present, an Origin that match the tunnel's public name or this machine's loopback name. Anything else gets 403 before the secret is checked. A tunnel that rewrites the Host header would be refused too, so the viewer would show 403 instead of working; the session then fails without exposing anything. Other remote limits are in the Remote desktop section.
* **Persistence.** The bot creates no Windows service, scheduled task, registry Run key or Startup entry. Scheduled tasks and reminders live in the bot's own job file and run only while the bot is running, each occurrence needing approval.
* **Dependencies.** `requirements.txt` pins exact versions of the four runtime packages. Optional voice packages are listed separately.

**Not enforced: read these**

* **Approved actions run with your Windows account's rights.** `powershell` (including `!`-prefixed commands), `open` of installed programs, and `write_file` are not sandboxed. The `allowed_roots` setting confines ordinary file actions only when you set it. When it is empty, `read_file`, `write_file`, `list_dir` and `send_file` (for non-protected files) can reach any folder your account can access. Approve only plans you have read, including their exact parameters.
* **Protections are name-based.** A copy of a credential file under another name, or a secret typed into a message, is not detected. The fingerprint detects accidental change; it is not a signature.
* **Telegram keeps what it receives.** Read, send and screenshot results, uploads, plans, and remote-session notices are stored by Telegram. The link message is protected from forwarding only inside the Telegram app.
* **Token storage.** `token.txt` is plain text. The per-user store uses DPAPI on Windows when available; without it, the token is stored in plain text with owner-only permissions.
* **`cloudflared` download.** The program downloads only from the official `github.com/cloudflare/cloudflared/releases/` path, follows only the GitHub asset redirect hosts, caps the size, and runs `cloudflared --version` before use. The default URL is `releases/latest`, so the version changes over time. Set `[remote] cloudflared_sha256` to a published SHA-256 to pin the exact binary; the check is off when that setting is empty.
* **Cloud planner privacy (default `mode = cloud`).** After a separate confirmation, your request text and system context (local date and time, OS name and version, home folder path, working folder path) are sent to `text.pollinations.ai`. Planner requests ignore proxy environment settings and do not follow redirects. Only literal loopback Ollama addresses (`127.0.0.1`, `::1`) count as local.
* **Remote desktop limits** are in the Remote desktop section: flaky free tunnels, UAC and secure-desktop windows out of reach, slower frame rates on weak PCs, and a link that must stay private.
* **Windows-only features are unverified here.** See the note at the top of this section.

## Actions

| Action | Effect / notes |
|---|---|
| `reply` | Answer only; no action approval needed |
| `system_info`, `list_processes` | System report; processes by CPU, memory or name |
| `kill_process` | Stop by PID or name; critical and configured protected names, PID ≤ 4, and assistant/parent PID are refused (best-effort process-name check) |
| `open`, `open_url` | Open an HTTP(S)/mailto link or a named program or file. `open` refuses UNC paths, URI schemes such as `ms-msdt:`, `file:`, `search-ms:` and `shell:`, scripts and shortcuts (`.ps1`, `.bat`, `.cmd`, `.vbs`, `.js`, `.lnk`, `.msi`…), and credential files |
| `screenshot`, `window_screenshot` | Approved JPEG photo with visible indicator; window capture never falls back to the full screen |
| `watch_start`, `watch_speed` | Approved visible full-screen sharing session or interval change |
| `lock_screen`, `notification`, `volume` | Lock, desktop notification, or volume key presses; volume needs an explicit mode (`mute`/`unmute`/`toggle`/`up`/`down`/`set`) or a numeric `level` — unknown or missing modes change nothing |
| `clipboard_get`, `clipboard_set` | Read or replace clipboard text |
| `read_file`, `write_file`, `list_dir` | Read/write text or list a folder; writes can overwrite |
| `find_files`, `send_file`, `receive_file` | Search, send a file to Telegram, or save an uploaded document after approval; confined to configured roots |
| `reminder`, `scheduled_task` | Persist reminders or PowerShell commands for later (`+10m`, `+2h`, `18:30`, `YYYY-MM-DD HH:MM`; some Arabic relative units supported) |
| `memory_update`, `memory_read` | Confirmed local note, preference and macro updates or note/macro listing |
| `briefing_schedule`, `briefing` | Schedule a daily approval request or read and send the briefing after approval |
| `job_cancel`, `jobs_read`, `audit_read` | Confirmed job deletion or transmission of job/audit metadata |
| `reminder_delivery`, `cloud_plan` | Confirm delivery of a reminder or sending a request to an external planner |
| `powershell`, `power` | Run a command; request shutdown, restart, sleep, hibernate or cancel |

Windows-specific actions need Windows and a usable desktop session. File transfers are also subject to Telegram's own limits; uploads can be refused by Telegram before the assistant sees them. Uploaded files get a sanitized unique name in `downloads_dir`; nothing is downloaded to the destination until the plan is approved.

## Commands and Jarvis-style features

| Command | What it does |
|---|---|
| `/screen`, `/window [title]` | Propose one full-screen or window-only JPEG photo; approve each capture. Without a title the foreground window at capture time is used. |
| `/watch start [seconds] [minutes]` | Propose a temporary full-desktop sharing session; default 3 seconds and 10 minutes. |
| `/watch speed N`, `/watch stop` | Propose an interval change (2–60 seconds) or stop immediately without approval. |
| `/remote start [minutes]` | Propose a private remote-desktop session with **full view and mouse/keyboard control** for 1–60 minutes (default 15). One approval starts it. See [Remote desktop](#remote-desktop-remote). |
| `/remote status`, `/remote stop` | Show the session state (never the link), or end it now and close the tunnel. |
| `/note TEXT` | Propose adding a note. Approve to save it locally. |
| `/notes` | Propose sending your saved numbered notes to this chat. |
| `/forget N` | Propose deleting note number N; checks it has not changed. `/forget language` removes the preference; `/forget all` removes notes, macros and preferences (not jobs). |
| `/language ar` or `/language en` | Propose saving a fallback language preference. Arabic or English detected in an ordinary request takes precedence; raw command output may remain in its original language. |
| `/briefing now` | Propose reading and sending date, battery when available, disk space, pending reminders and notes. |
| `/briefing daily 08:30` | Propose scheduling a daily local-time approval request. The briefing itself is sent **only after a fresh confirmation each day**. |
| `/chain open chrome ; take a screenshot` | Build one offline, ordered, confirmation-gated plan. Each semicolon-separated step must be recognized; an unrecognized step rejects the whole chain. |
| `/macro save morning routine` | After proposing a plan, save its exact steps as a macro. The save itself needs approval and replaces the original pending plan. |
| `/macro run morning routine` | Propose the saved steps as a new plan; approve before they run. `/macro delete NAME` deletes after approval; `/macros` proposes sending names. |
| `/jobs`, `/canceltask CODE`, `/logs` | Propose listing jobs, cancelling a job, or sending recent audit metadata; each needs approval. |
| `/start`, `/help`, `/id`, `/status`, `/privacy`, `/cancel`, `/panic` | Introduction, diagnostics, privacy, or cancel/abort. |
| `/yes CODE`, `/no CODE`, `/approve CODE`, `/deny CODE` | Approve/deny a pending plan; plain `approve CODE`, `deny CODE`, `موافق CODE` and `رفض CODE` work too. |

Plans expire after 15 minutes; deny or expiry means a due one-time reminder or command is not delivered/run and is not retried automatically. A due daily job waits if another plan from that requester is pending; missed daily occurrences are not replayed in a flood. `/panic` prevents queueing while active. Briefing/job schedules live only in the assistant, not Windows Task Scheduler; the bot must run for them to be queued.

### Visible screen sharing

`/screen` and `/window [title]` each produce a plan; approve with Yes or `approve CODE` before any capture. A title is matched case-insensitively; without one the foreground window is selected immediately before showing the indicator. Ambiguous titles select the first match. Some windows block capture or produce blank images; no full-screen fallback is attempted. A visible red topmost indicator is required during capture and upload, including one-shot photos. Its local **Stop screen sharing** button ends the operation.

`/watch start [seconds] [minutes]` proposes sharing the full desktop, including all visible screens. Defaults are **3 seconds and 10 minutes**; bounds are 2–60 seconds and 1–60 minutes. Approve once to start. The assistant sends a start notice, a photo, then updates that message with resized JPEG frames (maximum width 1280). `/watch speed N` proposes an approved change; `/watch stop` or `/panic` cancels immediately. A stop notice is sent when possible. Frames are sequential: slow uploads skip missed frames, Telegram rate-limit responses cause backoff, and failed edits can fall back to a fresh photo no more than once every 30 seconds. Network errors may prevent notices or updates. If the indicator closes, disappears, or stops reporting visibility, sharing stops; local GUI scheduling and heartbeat detection are best effort, not a tamper-proof guarantee. The remote stop cancels in-progress work, but an upload already sent to Telegram may still arrive and cannot be recalled. Telegram itself can retain photos and message history. No screenshot file is created; in-memory buffers are discarded after use. Capture requires a private chat whose ID equals the allowed requesting user ID. This feature uses no keylogger, webcam, or microphone. The separately optional voice-note feature accepts audio users deliberately send through Telegram; it does not activate the PC microphone.

### Remote desktop (`/remote`)

`/remote start [minutes]` proposes a session that gives **full view and control** of this PC: you see the screen and your mouse and keyboard drive it, for the chosen time (1–60 minutes, default 15). Only the allowed user in a private chat can request it, and nothing starts until you approve the plan with Yes or `approve CODE`. The approval text states the time and that control is included.

After approval the assistant:

1. shows an **always-on-top red banner** on the PC with the time remaining, the stop hint `Ctrl+Alt+Shift+Q`, and a **Stop** button;
2. starts a viewer server bound to `127.0.0.1` only and opens a public tunnel to it (Cloudflare quick tunnel first; the `localhost.run` SSH tunnel is the fallback). If the Cloudflare program is missing, it is downloaded once from the official `github.com/cloudflare/cloudflared/releases` address into `tools\`, then must run and identify itself before use;
3. sends **one private link** to your chat in a separate message, marked as not forwardable (Telegram's protection only applies inside the Telegram app).

Open the link in a browser. It works **once**: the first visit locks the secret and gives that browser a session cookie. A second visit with the same link gets 403. Only one viewer connection is accepted at a time. The viewer shows the measured frame rate, latency, and remaining time.

The session ends when you press **Stop** on the banner, press `Ctrl+Alt+Shift+Q`, send `/remote stop`, send `/panic`, reach the time limit, the banner or tunnel stops responding, or the bot shuts down. Every ending revokes the link and cookie, releases held keys and mouse buttons, closes the viewer connection, and kills the tunnel program. Telegram gets a stop notice. `/remote status` never displays the link.

**Security design**

* Only the allowed user ID in a private chat can request a session. A session needs a fresh approval, and a stop cancels any pending request that has not been approved yet. Starting a session is not possible from free-text, planner output, or macros.
* The link secret is 32 or more random bytes, new for every session, and compared in constant time. Requests without the cookie or a valid secret get 403. Failed attempts are rate-limited: 5 failures within 60 seconds lock out further attempts for 60 seconds, and 25 failures in total end the session.
* When the browser sends an Origin header (all browsers do for WebSockets), the connection is accepted only from the tunnel's own address. The viewer page loads no external resources and sends a restrictive content-security policy.
* Viewer input is validated (types, ranges and key names), rate-limited, and sent only while the session is active. Typed text is never logged.
* Nothing is written to disk: screen frames stay in memory, and the audit log records only timestamps, counts, and short reason labels (no secrets, links, keystrokes, or screen content).
* No hidden mode, no autostart, and no persistence: a restart ends any session. There is no keylogger, no microphone, and no webcam use.

**Limits and risks: please read before using**

* **Free tunnels are flaky.** Cloudflare quick tunnels and `localhost.run` have no uptime guarantee and can be slow, refuse to start, or drop mid-session. If no tunnel opens, nothing is exposed and the bot says so. The session also ends if the tunnel dies.
* **The localhost.run fallback is unverified live.** Its free service terms and sign-in requirements can change (its welcome text has mentioned SSH keys); if it refuses the connection, only the Cloudflare tunnel is available.
* **Tunnel providers carry the traffic.** Screen content and typed input pass through the tunnel provider's network. Do not use a session for sensitive material you would not give that provider.
* **The link must stay private.** Anyone who gets the link can use it until it is first opened; after that, anyone holding the browser's cookie can view and control the PC until the session ends. Forwarding it is equivalent to handing over the PC. Delete the link message after use, because Telegram keeps chat history.
* **UAC prompts and admin or secure-desktop windows cannot be controlled.** Windows blocks normal programs from sending input there, so the viewer can see nothing and the assistant's input is refused. Ctrl+Alt+Del and the lock screen are out of reach.
* **Weak PCs lower the frame rate.** The target is 5–10 frames per second with adaptive JPEG quality (maximum width 1280). Slow CPUs, slow networks, and high-resolution screens reduce both frame rate and detail, and add latency.
* **Some keys may not reach the PC.** Browser shortcuts and the Windows key may be intercepted by the browser or Windows.
* **The hotkey is unverified live.** `Ctrl+Alt+Shift+Q` is registered through Windows `RegisterHotKey` in the banner process. This could not be tested in the development sandbox, so confirm it on your PC. The Stop button and `/remote stop` do not depend on it.
* **Windows-only.** Capture, input, the banner, PowerShell, and live Telegram behavior can only be confirmed on the target Windows PC. The offline tests cover the logic around them.
* **Risks overview:** a leaked link or a compromised Telegram account gives control of the PC for as long as the session lasts (keep the default 15 minutes); tunnel providers see the traffic; the `cloudflared` download depends on GitHub and optionally on the SHA-256 pin in `[remote]`; if the bot process is killed abruptly, the tunnel program is stopped by a Windows job object only on a best-effort basis.

### Optional local voice notes

Voice is optional and uses the **multilingual** faster-whisper tiny model locally on the CPU. In a Command Prompt opened in the project folder, after `install.bat`, run:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
.venv\Scripts\python.exe -c "from faster_whisper.utils import download_model; download_model('tiny', output_dir='models/faster-whisper-tiny')"
```

The second command explicitly downloads the model from Hugging Face; neither the model nor the optional packages are included in the ZIP. You may change `[voice] model_path` to another already-downloaded compatible local model. Restart the bot, then send a Telegram voice note (up to 5 MB and 2 minutes). Telegram carries the audio to the bot; the program reads it in memory and transcribes locally, with no automatic model download or cloud transcription. The transcript uses the offline keyword planner even if cloud mode is configured, so it understands fewer tasks. The recognized text appears in the plan for review; **speech recognition can be wrong**, especially with noise, accents or Arabic, so verify the action and exact parameters. Approval codes and slash commands must be typed, not spoken. If the optional package or model is missing, the bot gives a setup message instead of executing anything.

From a console in the project folder:

```bat
.venv\Scripts\python.exe main.py --check
.venv\Scripts\python.exe main.py --plan "show system information"
.venv\Scripts\python.exe main.py --set-token
.venv\Scripts\python.exe main.py --forget-token
.venv\Scripts\python.exe -m unittest discover -s tests
```

`--plan` only plans and prints; it does not execute and uses offline keywords when the configured planner is external. `--check` can contact Telegram and planner endpoints. `--set-token` validates the token over the network. `--forget-token` deletes only the stored token; `token.txt` and an environment variable are unaffected.

## Configuration

`install.bat` copies `config.example.ini` to `config.ini` only if absent. Review the template for every option. Paths relative to the project resolve against the project folder.

| Setting | Meaning |
|---|---|
| `[telegram] allowed_user_ids` | Comma-separated requesters |
| `[telegram] approver_user_ids`, `require_distinct_approver` | Approvers must be allowed IDs; optional different-person rule |
| `[planner] mode`, `cloud_url`, `cloud_model`, `cloud_timeout`, `cloud_cooldown_seconds` | Planner selection and cloud settings |
| `[ollama] use_ollama`, `base_url`, `model`, `timeout` | Local model and fallback |
| `[behaviour] max_plan_steps`, `command_timeout`, `max_output_chars`, `max_file_read_kb` | Step and output limits |
| `[behaviour] max_send_file_mb`, `protected_processes`, `blocked_patterns` | Transfer cap and extra guardrails |
| `[paths] workspace_root`, `allowed_roots` | Working directory and optional roots for ordinary file operations. If roots are empty, **transfer/search actions** still default to workspace root; ordinary file actions are unrestricted. PowerShell and `open` are not confined. UNC, device-name and alternate-stream paths and credential files are refused everywhere. |
| `[paths] downloads_dir` | Uploaded files location; must lie within allowed roots (or workspace root if roots are empty). Legacy screenshot-path settings are ignored for screen photos. |
| `[paths] jobs_file`, `log_file` | Persistent jobs and metadata-only audit log. Memory is `memory.json` next to the jobs file. These files are not tamper-proof; restrict OS permissions. |
| `[voice] model_path` | Local faster-whisper model directory; no automatic model download. |
| `[remote] default_minutes`, `max_minutes` | Remote session length (default 15, maximum 60). |
| `[remote] tunnel_order`, `tools_dir`, `cloudflared_sha256` | Tunnel providers in order, the on-demand tools folder, and an optional SHA-256 pin for `cloudflared.exe`. |

Scheduled jobs use the PC's local time and are checked about every 15 seconds while the program runs. Missed jobs can be queued after restart, but always need a new approval. Someone with write access to local job/memory files can alter what is proposed; review the exact plan, and protect these files from other users. The audit log rotates. Screen photos are held in memory, not saved as screenshot files; Telegram retains sent photos according to its own policies.

## Troubleshooting & limitations

* `python` not found: reinstall Python with PATH enabled; open a new console.
* Bot silent: check IDs and token, start a private chat, and ensure only one bot instance is polling. Revoke a leaked token with BotFather and run `set_token.bat`.
* Cloud throttled or down: the bot attempts its configured fallback; wait or choose local/keywords. Ollama must be installed and the named model pulled.
* Screenshot black or GUI action fails: unlock the machine and use an interactive desktop session. Some protected, minimized or hardware-accelerated windows may be black or fail; no full-screen fallback is used.
* Approver not receiving the plan: they must start the bot first, and their numeric ID must match `approver_user_ids`.
* Check `logs\assistant.log`, `/status`, and `check_setup.bat`; current logs are metadata-only, but old logs may contain sensitive information.
* Voice setup fails: check that the optional package and local model were installed in this project; install the pinned PyAV dependency from `requirements-voice.txt`. Some machines or Python versions may not have compatible wheels.
* "outside the allowed folders" for a folder you did allow: make sure the path exists; every configured root and requested path is normalized the same way (symlinks, Windows 8.3 short names like `C:\Users\RUNNER~1\…` vs `C:\Users\runneradmin\…`, and case differences on Windows), so both spellings of the same folder are accepted.
* Remote session does not open: check the internet connection, then retry; `main.py --check` reports cloudflared and ssh availability. Free tunnels can fail at any time.
* Remote viewer shows a 403: the link was already used, expired, or stopped. Start a new session with `/remote start`.
* Garbled console output or `UnicodeEncodeError` on Windows: entry points reconfigure stdout/stderr to UTF-8 with `errors=replace`; the installers also run `chcp 65001`.

The offline automated tests exercise planner validation, mocked networking, approval flow, action safety, the .bat launchers' cmd block rules and Windows path/encoding handling. The remote-desktop tests cover secret handling, the single-use lock, expiry, rate limits, the loopback server and WebSocket framing, frame-rate adaptation, input validation, banner and tunnel protocol parsing, start authorization, and stop/panic teardown, with banner, tunnel, capture, and input replaced by fakes. Windows APIs, DPAPI, GUI interaction and real Telegram end-to-end behavior require testing on the target Windows PC; these cannot be guaranteed by the offline tests.

To uninstall, run `main.py --forget-token` before deleting the project (and remove `token.txt` yourself), remove any startup shortcut, remove `%APPDATA%\TelegramPCAssistant` if desired, and delete/revoke the bot in BotFather.


To rebuild a release archive, run `python build_zip.py` from the project. The builder uses a source allowlist and explicitly excludes `token.txt`, local configuration, token stores and runtime files. Always verify any archive before sharing it.
