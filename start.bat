@echo off
setlocal
cd /d "%~dp0"
title Scratch Comment Bot - Web UI

rem Avoid collisions with other local web apps that may already use port 8765.
set PORT=118876

echo ================================================
echo  Scratch Comment Bot - Web UI
echo  http://127.0.0.1:%PORT%/
echo  Browser will open automatically.
echo ================================================
echo.

if not exist "start_browser.bat" (
    echo start_browser.bat was not found.
    echo Run this file from the repository root.
    echo.
    pause
    exit /b 1
)

call "start_browser.bat"
exit /b %errorlevel%
