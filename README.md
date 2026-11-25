# Telegram PC Assistant 3.1

A Windows 10/11 program that turns Arabic or English Telegram requests into reviewable PC actions. It runs as **your Windows user**, uses Telegram polling and a cloud, local Ollama, or simple keyword planner. Review every plan before approving it; the program is not a security sandbox.

## Quick setup

Prerequisites: Windows 10/11, Python 3.10+ installed with **Add Python to PATH**, and a Telegram bot token from **@BotFather** (`/newbot`).

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

## Actions

| Action | Effect / notes |
|---|---|
| `reply` | Answer only; no action approval needed |
| `system_info`, `list_processes` | System report; processes by CPU, memory or name |
| `kill_process` | Stop by PID or name; critical and configured protected names, PID ≤ 4, and assistant/parent PID are refused (best-effort process-name check) |
| `open`, `open_url` | Open a target or an HTTP(S) URL; `open` can launch programs |
| `screenshot`, `window_screenshot` | Send a PNG; if window capture fails on Windows it may fall back to **the full screen** |
| `lock_screen`, `notification`, `volume` | Lock, desktop notification, or volume key presses; mute/unmute are toggles, not state-aware |
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
| `[paths] workspace_root`, `allowed_roots` | Working directory and optional roots for ordinary file operations. If roots are empty, **transfer/search actions** still default to workspace root; ordinary file actions are unrestricted. PowerShell and `open` are not confined. |
| `[paths] downloads_dir`, `screenshots_dir`, `keep_screenshots` | File locations and screenshot retention. `downloads_dir` must lie within allowed roots (or workspace root if roots are empty). |
| `[paths] jobs_file`, `log_file` | Persistent jobs and metadata-only audit log. Memory is `memory.json` next to the jobs file. These files are not tamper-proof; restrict OS permissions. |
| `[voice] model_path` | Local faster-whisper model directory; no automatic model download. |

Scheduled jobs use the PC's local time and are checked about every 15 seconds while the program runs. Missed jobs can be queued after restart, but always need a new approval. Someone with write access to local job/memory files can alter what is proposed; review the exact plan, and protect these files from other users. The audit log rotates; screenshots may remain on disk if `keep_screenshots = true`.

## Troubleshooting & limitations

* `python` not found: reinstall Python with PATH enabled; open a new console.
* Bot silent: check IDs and token, start a private chat, and ensure only one bot instance is polling. Revoke a leaked token with BotFather and run `set_token.bat`.
* Cloud throttled or down: the bot attempts its configured fallback; wait or choose local/keywords. Ollama must be installed and the named model pulled.
* Screenshot black or GUI action fails: unlock the machine and use an interactive desktop session. Window fallback can capture more than the requested window.
* Approver not receiving the plan: they must start the bot first, and their numeric ID must match `approver_user_ids`.
* Check `logs\assistant.log`, `/status`, and `check_setup.bat`; current logs are metadata-only, but old logs may contain sensitive information.
* Voice setup fails: check that the optional package and local model were installed in this project; install the pinned PyAV dependency from `requirements-voice.txt`. Some machines or Python versions may not have compatible wheels.

The offline automated tests exercise planner validation, mocked networking, approval flow and action safety. Windows APIs, DPAPI, GUI interaction and real Telegram end-to-end behavior require testing on the target Windows PC; these cannot be guaranteed by the offline tests.

To uninstall, run `main.py --forget-token` before deleting the project (and remove `token.txt` yourself), remove any startup shortcut, remove `%APPDATA%\TelegramPCAssistant` if desired, and delete/revoke the bot in BotFather.


To rebuild a release archive, run `python build_zip.py` from the project. The builder uses a source allowlist and explicitly excludes `token.txt`, local configuration, token stores and runtime files. Always verify any archive before sharing it.
