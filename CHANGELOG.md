# Changelog

Plain-language summary of the work from Round 6 to Round 12. The version string is still `3.1.0`; no version bump was made in these rounds.

Each round lists what a user sees. The "Not yet verified" notes say what the tests could not prove on a real Windows PC. Windows-only tests run on the GitHub `windows-latest` runner and skip on other systems.

## Round 6: remote desktop and robustness

* **Remote desktop:** the remote server now has an end-to-end test. It starts the real HTTP server on localhost and calls it with a real HTTP client.
* **Robustness:** a set of fixes for crashes and stuck states in the bot's background work. A few reported problems could not be reproduced and were left as they were.
* **Windows file permissions:** the Windows test for the owner-only token file was corrected. The product code was not changed for it.

Not yet verified: the remote session on a real PC with a real tunnel.

## Round 7: outbound redaction and Windows tests

* **Token safety:** every message the bot sends to Telegram now passes through one central filter that removes the bot token. A token pasted into a chat or appearing in an error is replaced with `[REDACTED]`.
* **Windows-only tests:** a first set of tests that run only on Windows (`tests/test_windows_real.py`). They use real files, real `icacls` and real sockets, and they never touch the clipboard, screen, keyboard or mouse.
* **Folder names:** a folder with Arabic letters in its name no longer breaks the reading of Windows access rules.

Not yet verified: the Windows-only tests on a real PC.

## Round 8: file paths, pause and administrator refusal

* **Read files:** `read_file` refuses to read anything when no allowed folder is set.
* **Send files:** `send_file` follows symbolic links and junctions, then checks the real destination against the allowed folders. A link that leads outside them is refused.
* **Receive files:** `receive_file` refuses an upload that is too large, using the size Telegram reports before the download starts. It also checks the destination folder.
* **`/pause` and `/resume`:** while paused, the bot replies to each request with a short "paused" notice and runs nothing. `/resume`, `/panic` and `/status` still work. The pause survives a restart. Only the allowed user can use these commands.
* **Administrator refusal:** the bot refuses to run with administrator rights. It prints and sends a clear message, then exits with code 5. There is no override. `main.py --check` reports the state.
* **Token file permissions:** the Windows lock-down of the token file is now one PowerShell script. Only the current user keeps access.
* **Approval gates:** unchanged. The bot does not bypass UAC, does not skip signature or hash checks, and does not hide installs.

Not yet verified: the lock-down on a real PC beyond the mocks and the CI runner; a real elevated process (tested with a stub only).

## Round 9: health check, app install and PowerShell launcher

* **`/health`:** a read-only PC check covering disk, memory, uptime, restart pending, Windows Defender, firewall and the Windows Update service. It needs no approval. Any item that cannot be read shows "unknown" with a short reason. It never shows user names, full paths or serial numbers, only drive letters.
* **`/install` and `/uninstall`:** install or remove an app through winget. Only apps on a built-in allow-list of ten are accepted (7-Zip, VLC, Notepad++, Git, Python, Chrome, Firefox, VS Code, Zoom, Telegram). Each needs approval and a UAC prompt on the PC, which the user must accept. The bot never bypasses UAC.
* **WhatsApp removed:** WhatsApp is not in the winget source (it is sold only in the Microsoft Store), so it was removed from the allow-list, the code and the docs. The README now says Store-only apps are not supported.
* **PowerShell launcher:** all PowerShell calls go through one launcher. It starts Windows PowerShell by its full path, and removes the variables that broke token saving when the bot ran under PowerShell 7. Error text is shortened and sanitized before it reaches a user.
* **Fixes:** a possible `NameError` in the sharing code; raw error output shown to users in the `powershell` action; a test that ran real PowerShell; path fragments left in error messages; progress lines leaking from winget output; wrong text in the health disk and firewall results; the zip builder dropping `assistant/data/`; a boot-time parser that depended on the Windows language; and a log-file order problem in test cleanup.

Not yet verified: winget and `/health` on a live Windows PC (stub tests only); the exact winget uninstall flags on a live PC.

## Round 10: quality audit

* **Rate limits:** a `429` reply from the cloud planner now waits for the time the service asks for (clamped to 1 to 30 seconds) and then sends the request once more. Before, it was dropped at once.
* **Long replies:** message splitting now counts characters the way Telegram does, so long replies are no longer cut too late.
* **Token across a split:** a token that straddled two parts of a long reply could slip past the filter. The whole text is now filtered before it is split.
* **Pasted tokens:** a token pasted into a request is refused before it reaches the cloud planner.
* **Log level:** an unknown `log_level` in `config.ini` is now a configuration error, not a silent fallback.
* **Help:** `/whoami` is listed in `/help`.
* **Docs and installer:** the README settings table now lists every setting. Installer failure messages are in Arabic too. The uninstall section has five clear steps.
* **Windows encoding:** twelve places that read or wrote non-ASCII text without an explicit encoding were fixed. A scanner test now checks for this.
* **Planner retry:** a connection reset during a planner request is retried once after a short pause. Safety checks are unchanged.
* **Test stability:** mock servers are threaded and tolerate dropped connections, and tests use free ports or patches instead of fixed closed ports.

Not yet verified: the Windows CI run on the final tree for every Python version.

## Round 11: real-Windows quality checks

* **Stuck process after a timeout:** a command that timed out could hang the bot when a child process still held its output pipe. The bot now kills the whole process tree and waits no more than 5 seconds.
* **Stop all:** stopping everything could leave grandchild processes running. It now kills the full tree.
* **File error messages:** `read_file` and `send_file` no longer show the full path in an OS error. They show a short reason.
* **Upload errors:** a failed upload names only the file, with a short reason.
* **Clipboard:** a clipboard timeout now gives a readable message instead of a raw error. A traceback fragment in Windows clipboard errors is no longer shown to users.
* **Locked log file:** if the audit log cannot be opened, for example because another program has it locked, the bot now logs to the console and still starts.
* **New Windows tests:** NTFS file names, escape attempts through links, process trees, locked files, and log rotation and token redaction in the log file.

Not yet verified: the Round 11 Windows-only tests have not been run on a real Windows machine; the tree kill and the locked-file behaviour are checked on Linux and by mocks only.

## Round 12: docs and end-to-end check

* **Troubleshooting guide:** `docs/TROUBLESHOOTING.md` in English and Arabic. It covers the bot not answering, token refusal, wrong user IDs, Python, `install.bat` failures, antivirus, the window closing, Telegram and proxies, expired approvals, `/remote` links, `/install` and the UAC prompt, revoking a token, uninstalling, and the log file. The README links to it.
* **Command and config tests:** every command in the bot's registry must appear in the English README and in the Arabic quick start, and every key in `config.example.ini` must be documented. Both checks run on every platform.
* **Arabic quick start:** now lists every command.
* **`[telegram] bot_token`:** documented as ignored. The token comes from `token.txt`, the environment, or the saved store.
* **Bot end-to-end test (Windows only):** the real bot object runs against a threaded stub Telegram server over real HTTP. It covers `/start`, `/health`, `/pause`, a file request while paused, `/resume`, the file request and its approval. A stranger's messages get no reply.

Not yet verified: the end-to-end test has not run on a real Windows PC yet; it checks the flow on the stub only, not the live Telegram service.
