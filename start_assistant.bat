@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Telegram PC Assistant

if not exist ".venv\Scripts\python.exe" (
    echo [X] The virtual environment is missing. Run install.bat first.
    echo بالعربية: بيئة التشغيل غير موجودة. شغّل install.bat أولًا.
    echo.
    pause
    exit /b 1
)

if not exist "config.ini" (
    echo [X] config.ini is missing.
    echo     Copy config.example.ini to config.ini ^(install.bat does this^).
    echo بالعربية: ملف config.ini غير موجود. شغّل install.bat لإنشائه.
    echo.
    pause
    exit /b 1
)

echo Starting Telegram PC Assistant...
echo (On the very first run it asks for your bot token with hidden input.)
echo Close this window or press Ctrl+C to stop the bot.
echo.
".venv\Scripts\python.exe" main.py
set "EXITCODE=%errorlevel%"
echo.
if "%EXITCODE%"=="4" (
    echo Telegram rejected the bot token - save a fresh one with:
    echo     .venv\Scripts\python.exe main.py --set-token
    echo بالعربية: رفض Telegram رمز البوت. احفظ رمزًا جديدًا بالأمر أعلاه.
    echo.
)
if "%EXITCODE%"=="3" (
    echo No bot token available - save one with:
    echo     .venv\Scripts\python.exe main.py --set-token
    echo بالعربية: لا يوجد رمز بوت. احفظ رمزًا بالأمر أعلاه.
    echo.
)
echo Assistant stopped (exit code %EXITCODE%).
echo Log file: logs\assistant.log
echo.
pause
exit /b %EXITCODE%
