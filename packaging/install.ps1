param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Garmin FIT Upload"),
    [string]$TemplatePath,
    [switch]$SkipDependencies,
    [switch]$NoShortcut,
    [switch]$NoLaunch,
    [switch]$NoTemplatePrompt
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$runtimeFiles = @(
    "garmin-fit-upload.exe",
    "garmin_auth_bridge.py",
    "garmin_converter.py",
    "fix_fit.py"
)

$packageRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "garmin-fit-upload.exe"))) {
    $packageRoot = Split-Path -Parent $PSScriptRoot
}
foreach ($file in $runtimeFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $file))) {
        throw "Package is incomplete: missing $file"
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
            & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
        }
    }
    return $null
}

function Select-GarminActivity {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Select one activity recorded by your Garmin watch"
    $dialog.Filter = "Garmin activity (*.fit;*.zip)|*.fit;*.zip"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw "A Garmin activity is required to create the local template."
    }
    return $dialog.FileName
}

function Install-Template([string]$Source, [string]$Destination) {
    $extension = [System.IO.Path]::GetExtension($Source)
    if ($extension -ieq ".fit") {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return
    }
    if ($extension -ine ".zip") {
        throw "Garmin template must be a FIT file or ZIP containing one FIT file."
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Source)
    try {
        $entries = @($archive.Entries | Where-Object { $_.Name.EndsWith(".fit", [System.StringComparison]::OrdinalIgnoreCase) })
        if ($entries.Count -ne 1) {
            throw "Garmin ZIP must contain exactly one FIT file."
        }
        $input = $entries[0].Open()
        $output = [System.IO.File]::Create($Destination)
        try {
            $input.CopyTo($output)
        } finally {
            $output.Dispose()
            $input.Dispose()
        }
    } finally {
        $archive.Dispose()
    }
}

if (-not $SkipDependencies) {
    $python = Find-Python
    if ($null -eq $python) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($null -eq $winget) {
            throw "Python 3.10+ is required. Install Python from python.org, then run Setup again."
        }
        Write-Host "Installing Python..."
        & $winget.Source install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "Python installation failed with exit code $LASTEXITCODE"
        }
        $python = Find-Python
        if ($null -eq $python) {
            throw "Python was installed but could not be found. Restart Windows, then run Setup again."
        }
    }

    Write-Host "Installing Garmin sign-in support..."
    & $python -m pip install --user --upgrade --disable-pip-version-check `
        "garminconnect>=0.3.5" "curl-cffi>=0.15"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE"
    }
}
$python = Find-Python
if ($null -eq $python) {
    throw "Python 3.10+ could not be found."
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
foreach ($file in $runtimeFiles) {
    Copy-Item -LiteralPath (Join-Path $packageRoot $file) -Destination $InstallDir -Force
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    (Join-Path $InstallDir "python-path.txt"),
    [string]$python,
    $utf8NoBom
)

if (-not $TemplatePath) {
    $bundled = Join-Path $packageRoot "garmin-template.fit"
    if (Test-Path -LiteralPath $bundled) {
        $TemplatePath = $bundled
    } elseif (-not $NoTemplatePrompt) {
        $TemplatePath = Select-GarminActivity
    }
}
if ($TemplatePath) {
    Install-Template (Resolve-Path -LiteralPath $TemplatePath).Path (Join-Path $InstallDir "garmin-template.fit")
} elseif (-not (Test-Path -LiteralPath (Join-Path $InstallDir "garmin-template.fit"))) {
    throw "A Garmin activity template is required."
}

if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut(
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "Garmin FIT Upload.lnk")
    )
    $shortcut.TargetPath = Join-Path $InstallDir "garmin-fit-upload.exe"
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "Convert MyWhoosh FIT files and upload them to Garmin Connect"
    $shortcut.Save()
}

Write-Host "Installed to $InstallDir"
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $InstallDir "garmin-fit-upload.exe") -WorkingDirectory $InstallDir
}
