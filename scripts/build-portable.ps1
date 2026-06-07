param(
    [string]$TemplatePath,
    [string]$OutputName = "garmin-fit-upload-windows-x64.zip"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$releaseExe = Join-Path $root "target\release\garmin-fit-upload.exe"
if (-not (Test-Path -LiteralPath $releaseExe)) {
    throw "Release executable not found. Run cargo build --release first."
}

$dist = Join-Path $root "dist"
$stage = Join-Path $dist "portable"
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$files = @(
    @{ Source = $releaseExe; Name = "garmin-fit-upload.exe" },
    @{ Source = (Join-Path $root "garmin_auth_bridge.py"); Name = "garmin_auth_bridge.py" },
    @{ Source = (Join-Path $root "garmin_converter.py"); Name = "garmin_converter.py" },
    @{ Source = (Join-Path $root "fix_fit.py"); Name = "fix_fit.py" },
    @{ Source = (Join-Path $root "packaging\Setup.bat"); Name = "Setup.bat" },
    @{ Source = (Join-Path $root "packaging\install.ps1"); Name = "install.ps1" },
    @{ Source = (Join-Path $root "packaging\README.txt"); Name = "README.txt" }
)
foreach ($file in $files) {
    Copy-Item -LiteralPath $file.Source -Destination (Join-Path $stage $file.Name) -Force
}

if ($TemplatePath) {
    $source = (Resolve-Path -LiteralPath $TemplatePath).Path
    $destination = Join-Path $stage "garmin-template.fit"
    if ([System.IO.Path]::GetExtension($source) -ieq ".fit") {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    } else {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($source)
        try {
            $entries = @($archive.Entries | Where-Object { $_.Name.EndsWith(".fit", [System.StringComparison]::OrdinalIgnoreCase) })
            if ($entries.Count -ne 1) {
                throw "Template ZIP must contain exactly one FIT file."
            }
            $input = $entries[0].Open()
            $output = [System.IO.File]::Create($destination)
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
}

$output = Join-Path $dist $OutputName
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Force
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $output -CompressionLevel Optimal
Get-Item -LiteralPath $output
