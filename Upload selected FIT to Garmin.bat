@echo off
setlocal
pushd "%~dp0"

if exist "dist\garmin-fit-upload.exe" (
    start "" "dist\garmin-fit-upload.exe"
    popd
    exit /b 0
)

if exist "target\release\garmin-fit-upload.exe" (
    start "" "target\release\garmin-fit-upload.exe"
    popd
    exit /b 0
)

echo Garmin FIT Upload has not been built.
echo Run: cargo build --release
pause
popd
