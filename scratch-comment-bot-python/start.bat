@echo off
setlocal
cd /d "%~dp0"
title Scratch Comment Bot - Python

if not defined PORT set PORT=8876
if not defined HOST set HOST=127.0.0.1
echo ================================================
echo  Scratch Comment Bot - Python
echo  http://%HOST%:%PORT%/
echo ================================================
echo.

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python

echo Python was not found.
echo Install Python 3.11 or newer, then run this file again.
goto failed

:use_py
echo Installing dependencies with the Python launcher...
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
echo Starting the server...
py -3 app.py
goto stopped

:use_python
echo Installing dependencies with python...
python -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
echo Starting the server...
python app.py
goto stopped

:install_failed
echo.
echo Dependency installation failed.
goto failed

:stopped
echo.
echo The server stopped. Exit code: %errorlevel%

:failed
echo.
pause
exit /b 1
