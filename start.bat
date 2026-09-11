@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py main_multi.py
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python main_multi.py
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
