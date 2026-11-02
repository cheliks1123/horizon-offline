# Builds a single-file Horizon Offline executable and packs the release zip.
#   .\build.ps1               installs PyInstaller, builds, packs
#   .\build.ps1 -SkipInstall  assumes PyInstaller is already installed (used by CI)

[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not $SkipInstall) {
        python -m pip install --upgrade pyinstaller
    }

    python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name HorizonOffline `
        --icon icon.ico `
        horizon_offline.py

    $package = Join-Path $PSScriptRoot 'dist/package'
    New-Item -ItemType Directory -Force -Path $package | Out-Null
    Copy-Item 'dist/HorizonOffline.exe', 'launcher.ini', 'README.md', 'icon.ico', 'LICENSE' $package -Force

    $zip = Join-Path $PSScriptRoot 'dist/HorizonOffline-win64.zip'
    if (Test-Path $zip) {
        Remove-Item $zip -Force
    }
    Compress-Archive -Path (Join-Path $package '*') -DestinationPath $zip

    Write-Host "Built $zip"
}
finally {
    Pop-Location
}
