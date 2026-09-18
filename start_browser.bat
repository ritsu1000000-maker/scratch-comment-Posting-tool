@echo off
setlocal
cd /d "%~dp0"
title Scratch Comment Bot - Browser UI

set PORT=8876
set HOST=127.0.0.1
set OPEN_BROWSER=1

if not exist "scratch-comment-bot-python\start.bat" (
    echo Python browser version was not found.
    echo Run this file from the repository root.
    echo.
    pause
    exit /b 1
)

call "scratch-comment-bot-python\start.bat"
exit /b %errorlevel%
