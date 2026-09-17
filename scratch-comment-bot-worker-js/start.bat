@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Scratch Comment Bot - Cloudflare Worker

echo =============================================
echo Scratch Comment Bot - Cloudflare Worker
echo =============================================
echo.

where node >nul 2>&1
if errorlevel 1 goto no_node
where npm >nul 2>&1
if errorlevel 1 goto no_npm

if not exist node_modules\wrangler\bin\wrangler.js (
  echo [INFO] Installing dependencies for the first run...
  call npm install
  if errorlevel 1 goto install_failed
)

echo.
echo [INFO] Starting local Worker.
echo [INFO] Open http://localhost:8787/ in your browser.
echo [INFO] Press Ctrl+C to stop it.
echo.
call npx wrangler dev --local
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo [ERROR] Wrangler exited with code %EXIT_CODE%.
echo.
pause
exit /b %EXIT_CODE%

:no_node
echo [ERROR] Node.js 18 or newer is required.
echo Install Node.js from https://nodejs.org/ and run this file again.
pause
exit /b 1

:no_npm
echo [ERROR] npm was not found in PATH.
echo Reinstall Node.js and ensure npm is included, then run this file again.
pause
exit /b 1

:install_failed
echo [ERROR] npm install failed. Check the message above and try again.
pause
exit /b 1
