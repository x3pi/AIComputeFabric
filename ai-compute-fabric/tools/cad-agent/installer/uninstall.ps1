<#
 Removes the AICF CAD Agent. Keeps job outputs and audit logs (the data folder) unless -Purge is given.
   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-InstallDir PATH] [-DataDir PATH] [-Purge]
#>
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "AICF-CAD\app"),
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA "AICF-CAD\data"),
  [switch]$Purge
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path (Join-Path $InstallDir "sw_agent.py"))) { Write-Host "Not installed at $InstallDir"; exit 0 }
# refuse to delete while a job is using the install
$busy = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like ("*" + $InstallDir + "*") -and $_.ProcessId -ne $PID }
if ($busy) { Write-Host "A process from this install is still running (PID $($busy[0].ProcessId)). Stop it first."; exit 1 }
Remove-Item -Recurse -Force $InstallDir
Write-Host "Removed $InstallDir (this also deleted the stored API key)."
if ($Purge -and (Test-Path $DataDir)) { Remove-Item -Recurse -Force $DataDir; Write-Host "Removed $DataDir" }
elseif (Test-Path $DataDir) { Write-Host "Kept $DataDir (job outputs and audit logs). Use -Purge to delete it." }
