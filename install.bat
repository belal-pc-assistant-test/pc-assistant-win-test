@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Telegram PC Assistant - Setup

echo ============================================================
echo   Telegram PC Assistant - first time setup
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 goto nopython

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [1/6] Found Python %PYVER%

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto oldpython

if exist ".venv\Scripts\python.exe" (
    echo [2/6] Virtual environment already exists - reusing it.
) else (
    echo [2/6] Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto venvfail
)

echo [3/6] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail

if exist "config.ini" (
    echo [4/6] config.ini already exists - left untouched.
) else (
    copy /y "config.example.ini" "config.ini" >nul
    echo [4/6] Created config.ini ^(allowed user id: 7823157087^).
)

echo [5/6] Running self check ...
echo.
".venv\Scripts\python.exe" main.py --check
echo.

echo [6/6] Bot token
echo.
if exist "token.txt" (
    echo   token.txt found. No token prompt; the file remains in place.
    echo   Keep this plaintext file private.
    goto after_token
)
echo   Optional: save the token in the per-user store instead of token.txt.
echo   You can also skip this and enter it at first startup.
set "SAVE_TOKEN="
rem An interactive read via set /p: on EOF (closed stdin) it simply
rem continues, so this installer never hangs in non-interactive runs.
set /p "SAVE_TOKEN=Save your bot token now? [y/N] "
if /i not "%SAVE_TOKEN%"=="Y" goto skip_token
".venv\Scripts\python.exe" main.py --set-token
goto after_token

:skip_token
echo.
echo   Skipped. You can enter the token when you start the assistant.

:after_token
echo.
echo ============================================================
echo   Setup finished.
echo ============================================================
echo.
echo   NEXT STEPS
echo   1. If needed, correct allowed_user_ids in config.ini.
echo   2. Open your bot in Telegram, press Start, send /help.
echo   3. For later starts, double-click start_assistant.bat.
echo.
echo   NOTES
echo   - config.ini currently allows Telegram user id 7823157087.
echo     If that is not your id, change allowed_user_ids in config.ini.
echo   - Default planner is "cloud" (text.pollinations.ai, no API key):
echo     approval is required before requests leave the PC. For a local
echo     planner set  [planner] mode = local  in config.ini and install
echo     Ollama from https://ollama.com/download  (ollama pull qwen2.5:3b).
echo   - Cloud service may be unavailable or rate limited; when
echo     it is busy the assistant falls back to Ollama/keywords and says so.
echo.
if exist "token.txt" (
    echo Starting the assistant now. Keep this window open.
    echo If token.txt is empty or invalid, correct it and run start_assistant.bat.
    ".venv\Scripts\python.exe" main.py --no-token-prompt
    echo Assistant stopped.
)
pause
exit /b 0

:nopython
echo.
echo [X] Python was not found on PATH.
echo     Install Python 3.11 or newer from https://www.python.org/downloads/windows/
echo     IMPORTANT: tick "Add python.exe to PATH" in the installer.
goto end

:oldpython
echo.
echo [X] Python 3.11 or newer is required. Please update Python.
goto end

:venvfail
echo.
echo [X] Could not create the virtual environment.
echo     Try: python -m pip install --upgrade virtualenv
goto end

:pipfail
echo.
echo [X] Dependency installation failed. Check your internet connection and retry.
goto end

:end
echo.
pause
