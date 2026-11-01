# Tao token co dinh (chi user hien tai doc duoc) va shortcut "Wagent" tren Desktop.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tok  = Join-Path $root 'web.token'
$env:PYTHONPATH = Join-Path $root 'src'
& (Join-Path $root 'venv\Scripts\python.exe') -c "from wagent.cli import load_token; load_token(r'$tok')"
icacls $tok /inheritance:r /grant:r "$($env:USERNAME):(F)" | Out-Null

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Wagent.lnk'
$sh  = New-Object -ComObject WScript.Shell
$s   = $sh.CreateShortcut($lnk)
$s.TargetPath       = Join-Path $root 'venv\Scripts\pythonw.exe'
$s.Arguments        = '"' + (Join-Path $root 'scripts\launch.pyw') + '"'
$s.WorkingDirectory = $root
$s.IconLocation     = (Join-Path $root 'wagent.ico') + ',0'
$s.Description      = 'Wagent - dieu khien Windows bang model local'
$s.Save()
Write-Output "shortcut: $lnk"
Write-Output ("target: " + $s.TargetPath + " " + $s.Arguments)
