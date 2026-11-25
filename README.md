# Telegram PC Assistant

Control your Windows PC from Telegram. You send a request, the assistant shows a plan, and **nothing runs until you approve it**. It runs with **your** Windows account rights. It is not a sandbox, so read the security notes before use.

---

## البدء السريع

1. ثبّت **Python 3.11** أو أحدث من <https://www.python.org/downloads/windows/> وفعّل خيار **Add python.exe to PATH** في الشاشة الأولى.
2. انقر نقرًا مزدوجًا على **`install.bat`** وانتظر حتى تظهر رسالة *Setup finished*.
3. أنشئ ملفًا نصيًا اسمه **`token.txt`** بجانب `install.bat` واكتب فيه توكن البوت من @BotFather في سطر واحد فقط.
4. انقر نقرًا مزدوجًا على **`start_assistant.bat`** وأبقِ النافذة مفتوحة.
5. افتح البوت في تيليغرام، واضغط **Start**، ثم أرسل **`/start`**.

إذا لم يرد البوت، راجع قسم [Troubleshooting](#troubleshooting). الإعداد الافتراضي يسمح بالرقم `7823157087` فقط.

---

## Quick start

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/windows/>. On the first installer screen, tick **Add python.exe to PATH**.
2. **Double-click `install.bat`.** It creates `.venv`, installs the dependencies, creates `config.ini`, and runs a self check. Wait for *Setup finished*. If Python is missing or too old, the window says so and shows the download link.
3. **Create `token.txt`** next to `install.bat` (see [The bot token](#the-bot-token-tokentxt)).
4. **Double-click `start_assistant.bat`** and keep its window open.
5. **In Telegram**, open your bot, press **Start**, and send `/start`. Then send `/help`.

**Check your user ID.** The shipped default `allowed_user_ids` is `7823157087` (in `config.example.ini`). If that is not your Telegram numeric ID, edit `allowed_user_ids` in `config.ini`. The bot silently ignores every other account. Find your ID with @userinfobot.

---

## Requirements

* Windows 10 or 11 with an interactive desktop session.
* Python 3.11+ (3.11 or newer) with **Add python.exe to PATH**.
* A bot token from @BotFather (`/newbot`).
* Internet access for installation and Telegram. The default cloud planner also needs it.

## The bot token (`token.txt`)

`token.txt` is the simplest place for the token. The assistant reads it directly and never prints it.

* **File name:** exactly `token.txt`, in the project folder next to `install.bat`. **Not** `token.txt.txt`. Windows hides known extensions, so turn on **File name extensions** in File Explorer's *View* menu first.
* **Content:** plain text, one line, only the token from @BotFather. The first non-empty line is used; surrounding spaces and a UTF-8 BOM are removed.
* **Priority order:** `token.txt` first, then the `TELEGRAM_BOT_TOKEN` environment variable, then the per-user store, then a hidden prompt on first start.
* **Empty file:** an empty or whitespace-only file is ignored and the next source is tried. An unreadable file or an invalid first line stops startup with an error. It is never silently replaced.
* **Per-user store (optional):** `set_token.bat` saves the token under `%APPDATA%\TelegramPCAssistant`. `main.py --forget-token` deletes that stored copy only.
* **Keep it private.** Anyone who can read `token.txt` can control the bot. Never share or commit it. If it leaks, revoke it in @BotFather (`/revoke`), then save the new token.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Introduction and command list |
| `/id`, `/whoami` | Show your Telegram ID |
| `/status` | Bot state, planner, approvers, jobs |
| `/privacy` | What leaves the PC |
| `/cancel` | Cancel pending plans |
| `/panic` | Stop everything: cancel plans, stop screen sharing and remote sessions, kill the tunnel |
| `/yes CODE`, `/no CODE` | Approve or deny a plan (`/approve` and `/deny` also work) |
| `/screen`, `/window [title]` | One screenshot of the screen or of one window (needs approval) |
| `/watch start [seconds] [minutes]`, `/watch speed N`, `/watch stop` | Visible screen sharing. Defaults: every 3 s, for 10 minutes |
| `/remote start [minutes]`, `/remote status`, `/remote stop` | Remote desktop session. See [Remote desktop](#remote-desktop) |
| `/note TEXT`, `/notes`, `/forget N` | Save, list or delete notes |
| `/language ar` or `/language en` | Fallback language |
| `/briefing now`, `/briefing daily HH:MM` | Status summary now, or every day at that time |
| `/chain STEP ; STEP` | Several steps as one plan, separated by `;` |
| `/macro save NAME`, `/macro run NAME`, `/macro delete NAME`, `/macros` | Save and reuse step lists |
| `/jobs`, `/canceltask CODE`, `/logs` | List scheduled jobs, cancel one, or show recent audit metadata |

**Free-text requests** go through the planner, for example `show running processes` or `find PDF files in C:\Users\Me\Documents`. Prefix with `!` or `ps:` to request a raw PowerShell command. Voice notes work too (see [Voice notes](#voice-notes-optional)).

## How approval works

1. The bot replies with a **plan**: every step with its exact parameters, a risk label, a six-character **code**, and a fingerprint.
2. Approve with **✅**, or send `approve CODE` (or `/yes CODE`). Refuse with **❌** or `deny CODE`.
3. A code works **once**, only for the plan it was issued for, and expires after **15 minutes**. A newer plan from the same person replaces older pending plans.

Read the parameters before you approve. The planner can misread a request.

## Remote desktop

`/remote start [minutes]` asks you to approve **full view and mouse/keyboard control** of this PC for 1 to 60 minutes (default 15). Only your allowed ID can ask, and only in a private chat. Nothing starts before you approve.

After approval:

1. A **red, always-on-top banner** appears on the PC with the time left, a **Stop** button, and the hotkey `Ctrl+Alt+Shift+Q`.
2. A viewer server starts on `127.0.0.1` only, and a public tunnel opens to it: Cloudflare quick tunnel first, then `localhost.run`. If `cloudflared` is missing, it is downloaded once from the official GitHub release into the `tools` folder and must run before use.
3. The bot sends **one private link** in its own message. It cannot be forwarded.

Open the link in a browser. The link works **once**: the first visit locks it and gives that browser a session cookie. A second use gets 403. One viewer is accepted at a time.

The session ends on **Stop**, the hotkey, `/remote stop`, `/panic`, the time limit, or when the banner is closed. Every ending revokes the link and cookie, releases held keys and buttons, and kills the tunnel program. `/remote status` shows the state and whether a viewer is connected, never the link.

**Limits:**
* **Free tunnels are flaky.** Quick tunnels have no uptime promise. If no tunnel opens, nothing is exposed and the bot says so.
* **The tunnel provider carries the traffic.** Screen content and typed input pass through it.
* **Keep the link private.** Anyone holding it can use it until it is first opened. After that, anyone with that browser's cookie controls the PC until the session ends. Delete the message afterwards; Telegram keeps chat history.
* **UAC prompts, admin windows and the secure desktop cannot be controlled.** Input is refused there.
* **Weak PCs lower the frame rate** (target 5 to 10 fps; quality adapts).
* **Not yet verified live:** the banner hotkey (`RegisterHotKey`), the `localhost.run` fallback, and the whole flow with real Telegram and a real tunnel. The Stop button and `/remote stop` do not depend on the hotkey.

## Screen sharing

`/screen` and `/window` each produce a plan that you approve before any capture. `/window` uses the title you give; a window that blocks capture may come out black, and no full-screen fallback is attempted. A red indicator shows while a capture runs. `/watch` sends frames to the chat as photos at the interval you set; `/watch stop` or `/panic` ends it. Frames stay in memory; no screenshot file is written.

## Voice notes (optional)

Voice notes are transcribed locally with faster-whisper, after a one-time setup. From the project folder, after `install.bat`:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
.venv\Scripts\python.exe -c "from faster_whisper.utils import download_model; download_model('tiny', output_dir='models/faster-whisper-tiny')"
```

The second line downloads the model (not tested in this build). The model folder must match `[voice] model_path` in `config.ini`. Voice notes are limited to 5 MB and 2 minutes. Transcripts use the offline planner, not the cloud one. Check the plan before approving, since speech recognition can be wrong. Approval codes and `/` commands are not accepted from voice.

## Settings (`config.ini`)

`install.bat` creates `config.ini` from `config.example.ini` if it does not exist. Edit it with Notepad.

| Setting | Meaning |
|---|---|
| `[telegram] allowed_user_ids` | Telegram IDs allowed to use the bot, comma-separated |
| `[telegram] approver_user_ids`, `require_distinct_approver` | Who may approve plans (must also be allowed); optionally, not the requester |
| `[planner] mode` | `cloud` (default), `local` (Ollama) or `keywords` (offline rules) |
| `[planner] cloud_cooldown_seconds` | Pause after a cloud rate-limit reply (default 120) |
| `[ollama] base_url` | Local Ollama address (default `http://127.0.0.1:11434`) |
| `[paths] allowed_roots` | Folders file actions may use. **Empty = no restriction** (see Security) |
| `[behaviour] max_plan_steps`, `command_timeout`, `max_send_file_mb` | Limits on plans, commands, transfers (defaults 6 steps, 60 s, 45 MB) |
| `[voice] model_path` | Local Whisper model folder |
| `[remote] default_minutes`, `max_minutes`, `tunnel_order`, `tools_dir`, `cloudflared_sha256` | Remote defaults (15 and 60), tunnel order, download folder, optional SHA-256 pin |

## Privacy

* **Cloud planner (default `mode = cloud`):** before anything is sent, the bot asks for a separate confirmation. Then your request text and system context (local date and time, OS, home folder and working folder paths) go to the third-party service `text.pollinations.ai`. Planner requests ignore proxy settings and do not follow redirects.
* **`mode = local` or `mode = keywords`:** planning stays on this PC. Telegram still carries your messages, plans, results and any files.
* **Telegram bot chats are not secret chats.**
* **The audit log** (`logs\assistant.log`) stores metadata only: event names, IDs, counts and short reason labels. It does not store request text, notes, command output, screenshots or keystrokes. It rotates at 2 MB, keeping 5 old files.

## Security

**Enforced in code** (each covered by tests):
* Only allowed Telegram IDs in a private chat can use the bot. Groups, channels, inline queries and edited messages are ignored.
* Nothing acts without an approved plan. Voice, macros, reminders, scheduled jobs, forwarded text and replayed buttons cannot run a plan on their own. A plan runs at most once.
* Approval codes are tied to the exact steps, single-use, expire after 15 minutes, and are replaced by a newer plan.
* At most 50 plans can wait for approval. Each user may send 30 updates per 10 seconds. Requests over 2000 characters are refused.
* Planner output must match the schema and an action allowlist. `remote_start` can only be started with `/remote start`.
* Paths: UNC and network paths, reserved device names (`CON`, `NUL`, `COM1`…) and alternate data streams (`file.txt:stream`) are refused.
* Uploads: Telegram's declared size is checked before download. Received names are sanitized, and the file is written to a temporary name.
* File actions refuse the protected names `token.json`, `token.txt` and `.env`. Writes to the Windows Startup folder are refused. The token is never written to `config.ini`, plans or Telegram messages, and it is removed from audit text.
* Clipboard text and notification text reach PowerShell encoded as base64, not pasted into the script.
* Remote: the link secret is at least 32 random bytes and new for each session. Failed attempts are rate-limited: 5 failures in 60 seconds cause a 60-second lockout, and 25 failures in total end the session. Host and Origin headers are checked on every request. Input is accepted only after a viewer authenticates.
* The `cloudflared` download uses only the official `github.com/cloudflare/cloudflared/releases/` path, caps its size, and runs `cloudflared --version` before use.
* The bot creates no Windows service, scheduled task, Run key or Startup entry. Dependencies are pinned in `requirements.txt`.

**Limits (not enforced, or not fully reviewed):**
* **Approved actions run with your account's rights.** `powershell`, `!` commands, opening programs and `write_file` are not sandboxed. Unless you set `allowed_roots`, ordinary file actions can reach any folder your account can access. Read every plan before approving it.
* **Some file paths are not yet confined.** `read_file` has no roots check when `allowed_roots` is empty. `send_file` may follow a symlink outside the allowed folders. `receive_file` checks the size of the received file only after the download finishes, and it does not yet check the destination for symlinks before the final rename. These are open items.
* **Not yet reviewed in full:** text routing, macros, chains, the reminder and briefing queues, and the stored config and token file permissions.
* **Protections are name-based.** A copy of a credential under another name is not detected.
* **Telegram keeps what it receives**, including results, uploads and plans.
* **Not validated live on Windows:** PowerShell actions, clipboard, process control, DPAPI, screen capture, the banner, the `cloudflared` runtime, and live Telegram. These were tested with fakes in a Linux sandbox.
* **Cloudflared pin:** the default download is `releases/latest`, so the version changes over time. Set `cloudflared_sha256` to pin it.

## Troubleshooting

### The bot does not answer

1. **Is `start_assistant.bat` still running?** If it closed, start it again. Its window shows the reason it stopped.
2. **Are you in the bot's private chat?** Press **Start** there and send `/start`. Groups are ignored.
3. **Is another copy running?** Only one copy can receive Telegram messages with the same token. Close the other copy (on this PC or another) and start again.
4. **Is your ID allowed?** See [Wrong user ID](#wrong-user-id).
5. **Is the token valid?** Exit code **3** means no token was found: save one in `token.txt`, or run `set_token.bat`. Exit code **4** means Telegram rejected it: create a new one in @BotFather, then run `set_token.bat`.
6. **Can this PC reach Telegram?** Check the internet connection, and any firewall or proxy that blocks `api.telegram.org`.
7. **Run `check_setup.bat`** to repeat the self check. Then look in `logs\assistant.log`.

### Wrong user ID

The bot does not answer accounts that are not in `allowed_user_ids`. This is deliberate, so strangers get no reply.

* **Find your ID:** message @userinfobot. It replies with your numeric ID.
* **Fix it:** edit `allowed_user_ids` in `config.ini` (comma-separated), then restart with `start_assistant.bat`.
* **Check the log:** a stranger's attempt is logged as a warning with a `user_id` and the reason `unauthorised user`, at most once a minute per account. The message text is not logged.

### Planner rate limit

Cloud planning uses a free public service with per-IP limits. When it refuses, the plan note shows one of:

* `cloud planner rate limited (HTTP 429), paused for N s`
* `cloud planner free-tier limit (HTTP 402), paused for N s`

The bot then pauses cloud planning for `cloud_cooldown_seconds` (default 120, N is between 15 and 600 seconds). It uses the local Ollama model if configured, otherwise the offline keyword rules. To avoid waiting, use short, direct requests, or the commands that need no planner (`/screen`, `/watch`, `/status`). You can also set `[planner] mode = local` with Ollama, or `mode = keywords`.

### Other problems

* **Python not found, or too old:** `install.bat` says which case applies and shows the download page. Install Python 3.11 or newer with **Add python.exe to PATH**, then open a new window and run `install.bat` again.
* **Remote link shows 403:** the link was already used, has expired, or the session has stopped. Run `/remote start` again.
* **Remote session does not open:** check the internet connection and retry. `main.py --check` shows whether `cloudflared` and `ssh` are available.
* **Screenshot is black:** keep the PC unlocked with an interactive desktop, and note that some windows block capture.

## Uninstall

Delete `token.txt` yourself. Run `main.py --forget-token` to remove the stored token, delete `%APPDATA%\TelegramPCAssistant` if you want, delete the project folder, and revoke the bot in @BotFather.

## For developers

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m pyflakes main.py assistant tests build_zip.py
python build_zip.py
```

`build_zip.py` builds the release ZIP from an explicit allowlist. It excludes `token.txt`, `config.ini`, token stores and runtime folders. The tests run on Linux, with fakes for the Windows APIs, tunnels and capture. The Windows-only parts above still need a run on a real Windows PC.
