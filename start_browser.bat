@echo off
setlocal
cd /d "%~dp0"
title Scratch Comment Bot - Browser UI

if not exist "scratch-comment-bot-python\start.bat" (
    echo Python browser version was not found.
    echo Run this file from the repository root.
    echo.
    pause
    exit /b 1
)

call "scratch-comment-bot-python\start.bat"
exit /b %errorlevel%
