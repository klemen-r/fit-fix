param(
    [string]$DonorPath,
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
    @{ Source = (Join-Path $root "garmin_donor_spoof.py"); Name = "garmin_donor_spoof.py" },
    @{ Source = (Join-Path $root "garmin_pipeline.py"); Name = "garmin_pipeline.py" },
    @{ Source = (Join-Path $root "profile_config.py"); Name = "profile_config.py" },
    @{ Source = (Join-Path $root "fix_fit.py"); Name = "fix_fit.py" },
    @{ Source = (Join-Path $root "packaging\Install Garmin FIT Upload.bat"); Name = "Install Garmin FIT Upload.bat" },
    @{ Source = (Join-Path $root "packaging\install.ps1"); Name = "install.ps1" },
    @{ Source = (Join-Path $root "packaging\README.txt"); Name = "README.txt" }
)
foreach ($file in $files) {
    Copy-Item -LiteralPath $file.Source -Destination (Join-Path $stage $file.Name) -Force
}

if ($DonorPath) {
    $resolvedDonor = (Resolve-Path -LiteralPath $DonorPath).Path
    Copy-Item -LiteralPath $resolvedDonor -Destination (Join-Path $stage "23128003580.zip") -Force
}

$output = Join-Path $dist $OutputName
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Force
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $output -CompressionLevel Optimal
Get-Item -LiteralPath $output
