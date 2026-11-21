<# Removes CadFabric (private Python, shortcuts). Your settings (%APPDATA%\cadfabric) and STEP files (%LOCALAPPDATA%\cadfabric) are kept unless you pass -Purge. #>
param([string]$Prefix = $PSScriptRoot, [switch]$Purge)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path (Join-Path $Prefix 'python\python.exe')) -or -not (Test-Path (Join-Path $Prefix 'cadfabric.cmd'))) { throw "Refusing: '$Prefix' does not look like a CadFabric install folder." }
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    Get-ChildItem $dir -Filter 'CadFabric*.lnk' -ErrorAction SilentlyContinue | ForEach-Object {
        # only a shortcut that points into THIS install (another CadFabric install keeps its own)
        if ($sh.CreateShortcut($_.FullName).TargetPath.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) { Remove-Item -Force $_.FullName }
    }
}
if ($Purge) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $env:APPDATA 'cadfabric')
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $env:LOCALAPPDATA 'cadfabric')
}
Start-Sleep -Milliseconds 500
Remove-Item -Recurse -Force $Prefix
Write-Host 'CadFabric removed.'
