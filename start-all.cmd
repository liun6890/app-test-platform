@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
if errorlevel 1 (
    echo.
    echo Service startup failed. Review the message above.
    pause
)
exit /b %ERRORLEVEL%
