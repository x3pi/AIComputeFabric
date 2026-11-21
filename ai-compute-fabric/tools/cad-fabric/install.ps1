# Install cadfabric into its own virtualenv and run the doctor.  Usage: .\install.ps1 [-Prefix DIR] [-Python py]
param([string]$Prefix = (Join-Path $env:LOCALAPPDATA "cadfabric"), [string]$Python = "python")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = & $Python -c "import sys; print(int(sys.version_info[:2] >= (3,10)))"
if ($ok -ne "1") { throw "Python 3.10 or newer is required." }
New-Item -ItemType Directory -Force $Prefix | Out-Null
& $Python -m venv (Join-Path $Prefix "venv")
$vpy = Join-Path $Prefix "venv\Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet $here
$exe = Join-Path $Prefix "venv\Scripts\cadfabric.exe"
$cfg = Join-Path $env:APPDATA "cadfabric\config.json"
if (-not (Test-Path $cfg)) { & $exe init }
Write-Host "`nInstalled: $exe   (add $(Join-Path $Prefix 'venv\Scripts') to PATH to call it as 'cadfabric')"
Write-Host "Edit $cfg (url, model, key_file), then run doctor:"
& $exe doctor
