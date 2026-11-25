@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Telegram PC Assistant - Bot token

if not exist ".venv\Scripts\python.exe" (
    echo [X] Run install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py --set-token
echo.
pause
