@echo off
setlocal
set "SCRIPT=%~dp0fix_fit.py"

where pyw >nul 2>nul
if %ERRORLEVEL% == 0 (
    start "" pyw -3 "%SCRIPT%" %*
    exit /b
)

where pythonw >nul 2>nul
if %ERRORLEVEL% == 0 (
    start "" pythonw "%SCRIPT%" %*
    exit /b
)

where python >nul 2>nul
if %ERRORLEVEL% == 0 (
    python "%SCRIPT%" %*
    if not errorlevel 1 exit /b 0
    pause
    exit /b 1
)

echo Python 3.10 or newer is required.
echo Install from https://www.python.org/downloads/ and tick "Add Python to PATH".
pause
