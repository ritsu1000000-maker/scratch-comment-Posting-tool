@echo off
setlocal
cd /d "%~dp0"
title Scratch Comment Bot - Web UI

echo ================================================
echo  Scratch Comment Bot - Web UI
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
