@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -m pip install -r requirements.txt
    goto :end
)

echo Python was not found.
echo Install Python and enable "Add python.exe to PATH".
echo.
pause
exit /b 1

:end
echo.
pause
