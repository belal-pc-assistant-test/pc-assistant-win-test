@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Telegram PC Assistant - Setup

echo ============================================================
echo   Telegram PC Assistant - setup
echo ============================================================
echo.
echo   This window stays open until setup has finished or failed.
echo.

set "PY_CMD="
set "PYVER="

rem 1) Find Python 3.11 or newer: first "python", then the "py" launcher.
where python >nul 2>&1
if errorlevel 1 goto try_py_launcher
for /f "delims=" %%v in ('python -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
if not defined PYVER goto try_py_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto try_py_launcher
set "PY_CMD=python"
goto have_python

:try_py_launcher
where py >nul 2>&1
if errorlevel 1 goto check_old_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto check_old_python
set "PY_CMD=py -3"
for /f "delims=" %%v in ('py -3 -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
goto have_python

:check_old_python
if defined PYVER goto old_python
goto no_python

:have_python
echo [1/6] Using Python %PYVER%
echo.

rem 2) Virtual environment in .venv (created once, reused later).
if exist ".venv\Scripts\python.exe" goto venv_exists
echo [2/6] Creating the virtual environment in .venv ...
%PY_CMD% -m venv .venv
if errorlevel 1 goto venv_fail
goto venv_ready

:venv_exists
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto venv_broken
echo [2/6] Virtual environment already exists - reusing it.

:venv_ready
echo [3/6] Installing dependencies from requirements.txt (this can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pip_fail

rem 4) config.ini is created from the template only if it does not exist yet.
if exist "config.ini" goto config_kept
copy /y "config.example.ini" "config.ini" >nul
if errorlevel 1 goto config_fail
echo [4/6] Created config.ini from config.example.ini.
echo       The shipped allowed Telegram user ID is 7823157087. If that is not
echo       your ID, edit allowed_user_ids in config.ini. See README.md.
goto config_done

:config_kept
echo [4/6] config.ini already exists - left untouched.

:config_done
echo.

rem 5) Self check (it may contact Telegram and the planner).
echo [5/6] Running the self check ...
echo.
".venv\Scripts\python.exe" main.py --check
if errorlevel 1 echo       The self check reported a problem above. Fix it, then run check_setup.bat.
echo.

rem 6) Bot token. token.txt is read directly by the assistant and is never printed.
echo [6/6] Bot token
echo.
if exist "token.txt" (
    echo   token.txt found. No token prompt; the file stays where it is.
    echo   Keep this plain-text file private.
    goto after_token
)
echo   No token.txt yet. You can save the token in the private per-user store
echo   now, or create token.txt later (see the next steps below).
set "SAVE_TOKEN="
set /p "SAVE_TOKEN=Save your bot token now in the private store? [y/N] "
if /i not "%SAVE_TOKEN%"=="y" goto skip_token
".venv\Scripts\python.exe" main.py --set-token
set "STORED=1"
goto after_token

:skip_token
echo   Skipped. Create token.txt next to install.bat before you start the assistant.

:after_token
echo.
echo ============================================================
echo   Setup finished.
echo ============================================================
echo.
if exist "token.txt" goto start_now
if defined STORED goto stored_next

echo   NEXT STEPS
echo   1. Create a file named token.txt next to install.bat. Put only the bot
echo      token from @BotFather in it, on one line, and save it as plain text.
echo   2. Double-click start_assistant.bat and keep its window open.
echo   3. In Telegram, open your bot, press Start, then send /start.
echo.
goto end_ok

:stored_next
echo   NEXT STEPS
echo   1. Your token is saved in the private per-user store. Double-click
echo      start_assistant.bat and keep its window open.
echo   2. In Telegram, open your bot, press Start, then send /start.
echo.
goto end_ok

:start_now
echo   NEXT STEPS
echo   1. The assistant starts now. Keep this window open.
echo   2. In Telegram, open your bot, press Start, then send /start.
echo   Later starts: double-click start_assistant.bat.
echo.
".venv\Scripts\python.exe" main.py --no-token-prompt
echo.
echo The assistant stopped with exit code %errorlevel%.
echo If it did not work, read the Troubleshooting section of README.md.
goto end_ok

:no_python
echo.
echo [X] Python 3.11 or newer was not found on this PC.
echo.
echo     What to do:
echo     1. Open https://www.python.org/downloads/windows/
echo     2. Download the latest Python 3 installer and run it.
echo     3. On the FIRST screen, tick "Add python.exe to PATH" before you click Install.
echo     4. Wait for the installation to finish.
echo     5. Close this window and double-click install.bat again.
echo.
goto ask_open_python

:old_python
echo.
echo [X] Found Python %PYVER%, but Python 3.11 or newer is required.
echo.
echo     Install the newest Python 3 from https://www.python.org/downloads/windows/
echo     and tick "Add python.exe to PATH". Then run install.bat again.
echo     If the old version is still used, remove it in Windows Settings under Apps.
echo.

:ask_open_python
set "OPEN_PY="
set /p "OPEN_PY=Open the Python download page in your browser now? [y/N] "
if /i "%OPEN_PY%"=="y" start "" "https://www.python.org/downloads/windows/"
goto end_fail

:venv_fail
echo.
echo [X] Could not create the virtual environment in the .venv folder.
echo     Check that this folder is writable (not inside Program Files), then run install.bat again.
goto end_fail

:venv_broken
echo.
echo [X] The existing .venv folder cannot be used. It may come from an older Python or was moved.
echo     Delete the .venv folder next to install.bat, then run install.bat again.
goto end_fail

:pip_fail
echo.
echo [X] Installing the dependencies failed. Check your internet connection, then run install.bat again.
echo     The error details are shown above.
goto end_fail

:config_fail
echo.
echo [X] Could not create config.ini. Check that this folder is writable, then run install.bat again.
goto end_fail

:end_fail
echo.
echo Setup did not finish. Nothing has been started.
echo.
pause
exit /b 1

:end_ok
echo.
pause
exit /b 0
