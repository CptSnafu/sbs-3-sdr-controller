$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

python -m pip install --upgrade pyinstaller
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "SBS-3 SDR Control" `
    --paths "src" `
    "SBS3ControlGUI.py"

Write-Host "Built: $projectRoot\dist\SBS-3 SDR Control.exe"
Write-Host "The official FTDI D2XX driver (ftd2xx.dll) must remain installed."
