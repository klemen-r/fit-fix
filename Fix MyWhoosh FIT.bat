@echo off
setlocal
set "SCRIPT=%~dp0fix_fit.py"
set "ARGS=--mimic-garmin --inject-metrics"

where pyw >nul 2>nul
if %ERRORLEVEL% == 0 (
    start "" pyw -3 "%SCRIPT%" %ARGS% %*
    exit /b
)

where pythonw >nul 2>nul
if %ERRORLEVEL% == 0 (
    start "" pythonw "%SCRIPT%" %ARGS% %*
    exit /b
)

where python >nul 2>nul
if %ERRORLEVEL% == 0 (
    python "%SCRIPT%" %ARGS% %*
    pause
    exit /b
)

echo Python 3.10 or newer is required.
echo Install from https://www.python.org/downloads/ and tick "Add Python to PATH".
pause
