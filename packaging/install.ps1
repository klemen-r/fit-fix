param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Garmin FIT Upload"),
    [switch]$SkipDependencies,
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$runtimeFiles = @(
    "garmin-fit-upload.exe",
    "garmin_auth_bridge.py",
    "garmin_donor_spoof.py",
    "garmin_pipeline.py",
    "profile_config.py",
    "fix_fit.py"
)

$packageRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "garmin-fit-upload.exe"))) {
    $packageRoot = Split-Path -Parent $PSScriptRoot
}

foreach ($file in $runtimeFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $file))) {
        throw "Portable package is incomplete: missing $file"
    }
}

function Find-Python {
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        try {
            $resolved = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                $candidates += $resolved.Trim()
            }
        } catch {
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidates += $python.Source
    }
    $candidates += Get-ChildItem `
        -Path (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe") `
        -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName

    foreach ($candidate in $candidates | Select-Object -Unique) {
        try {
            $supported = & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
        }
    }
    return $null
}

if (-not $SkipDependencies) {
    $python = Find-Python
    if ($null -eq $python) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($null -eq $winget) {
            throw "Python 3.10+ is required and winget is unavailable. Install Python from python.org, then run this installer again."
        }
        Write-Host "Installing Python for the current user..."
        & $winget.Source install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "Python installation failed with exit code $LASTEXITCODE"
        }
        $python = Find-Python
        if ($null -eq $python) {
            throw "Python was installed but could not be found. Restart Windows, then run this installer again."
        }
    }

    Write-Host "Installing Garmin runtime components..."
    & $python -m pip install --user --upgrade --disable-pip-version-check `
        "garmin-fit-sdk>=21.205" "garminconnect>=0.3.5" "curl-cffi>=0.15"
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed with exit code $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
foreach ($file in $runtimeFiles) {
    Copy-Item -LiteralPath (Join-Path $packageRoot $file) -Destination $installDir -Force
}

$donor = Join-Path $packageRoot "23128003580.zip"
if (Test-Path -LiteralPath $donor) {
    Copy-Item -LiteralPath $donor -Destination $installDir -Force
    Write-Host "Installed private Garmin donor."
} else {
    Write-Warning "23128003580.zip was not beside the installer. Copy it into '$installDir' before converting."
}

if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Garmin FIT Upload.lnk"
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $installDir "garmin-fit-upload.exe"
    $shortcut.WorkingDirectory = $installDir
    $shortcut.Description = "Convert MyWhoosh FIT files and upload them to Garmin Connect"
    $shortcut.Save()
}

Write-Host "Installed to $installDir"
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $installDir "garmin-fit-upload.exe") -WorkingDirectory $installDir
}
