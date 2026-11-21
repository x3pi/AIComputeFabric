<#
 CadFabric offline installer (Windows 10/11 x64, current user, no administrator rights).
 Everything is inside this folder: an embeddable Python 3.12, a wheelhouse (CadQuery + dependencies) and the cadfabric wheel. Nothing is downloaded.
 It does NOT touch a Python you already have. It installs into -Prefix (default %LOCALAPPDATA%\CadFabric) and adds Start Menu / Desktop shortcuts.

 Optional pre-configuration for a fleet of machines (otherwise the app opens its Settings dialog on first run):
   -Url      OpenAI-compatible chat URL      -Model    model name      -Planner  off|nothink|think|codex|codex-relay
   -KeyFile  a file holding the API key (copied into the user's config folder, never passed on a command line)
   -RelayHost / -RelayWorkdir   for planner codex-relay
#>
[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'CadFabric'),
    [string]$Url, [string]$Model, [string]$Planner, [string]$KeyFile, [string]$RelayHost, [string]$RelayWorkdir,
    [switch]$NoDesktopShortcut, [switch]$NoShortcuts
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
function Step($m) { Write-Host ("`n== " + $m) -ForegroundColor Cyan }
function Run($exe, $argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw ("command failed (exit {0}): {1} {2}" -f $LASTEXITCODE, $exe, ($argList -join ' ')) }
}

if (-not [Environment]::Is64BitOperatingSystem) { throw 'CadQuery needs a 64-bit Windows.' }
if (-not [IO.Path]::IsPathRooted($Prefix)) { throw 'Prefix must be an absolute path.' }

Step 'Visual C++ runtime'
$vc = Test-Path (Join-Path $env:windir 'System32\msvcp140.dll')
if ($vc) { Write-Host 'present' }
else {
    $redist = Join-Path $here 'vc_redist.x64.exe'
    if (-not (Test-Path $redist)) { throw 'msvcp140.dll is missing and vc_redist.x64.exe is not in this folder. Install the Microsoft Visual C++ 2015-2022 x64 runtime and run again.' }
    Write-Host 'installing the Microsoft runtime (Windows will ask for permission)...'
    $p = Start-Process -FilePath $redist -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -Wait -PassThru
    if (@(0, 1638, 3010) -notcontains $p.ExitCode) { throw ('vc_redist failed with exit code ' + $p.ExitCode) }
}

Step ('Python 3.12 (private copy) in ' + $Prefix)
$pydir = Join-Path $Prefix 'python'
$py = Join-Path $pydir 'python.exe'
$site = Join-Path $pydir 'Lib\site-packages'
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (-not (Test-Path $py)) {
    $zip = Get-ChildItem $here -Filter 'python-3.12.*-embed-amd64.zip' | Select-Object -First 1
    if (-not $zip) { throw 'the embeddable Python zip is missing from the bundle' }
    New-Item -ItemType Directory -Force $pydir | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($zip.FullName, $pydir)
    $sig = Get-AuthenticodeSignature $py
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item -Recurse -Force $pydir
        throw 'python.exe in the bundle is not signed by the Python Software Foundation; refusing to install.'
    }
} else { Write-Host 'Python already there' }
if (-not (Test-Path (Join-Path $site 'pip'))) {
    # The embeddable build ignores site-packages until its ._pth file says so; pip is a pure-Python wheel (a zip), so unpacking it IS installing it.
    $pth = Get-ChildItem $pydir -Filter 'python3*._pth' | Select-Object -First 1
    [IO.File]::WriteAllLines($pth.FullName, @(($pth.BaseName + '.zip'), '.', 'Lib\site-packages', 'import site'))
    New-Item -ItemType Directory -Force $site | Out-Null
    $pip = Get-ChildItem (Join-Path $here 'wheelhouse') -Filter 'pip-*.whl' | Select-Object -First 1
    if (-not $pip) { throw 'the pip wheel is missing from the wheelhouse' }
    [IO.Compression.ZipFile]::ExtractToDirectory($pip.FullName, $site)
}
Run $py @('-m', 'pip', '--version')

Step 'Installing cadfabric and CadQuery (offline)'
Run $py @('-m', 'pip', 'install', '--no-index', '--find-links', (Join-Path $here 'wheelhouse'), '--no-warn-script-location', '--upgrade', 'cadfabric')

if ($Url -or $Model -or $Planner -or $KeyFile -or $RelayHost -or $RelayWorkdir) {
    Step 'Writing settings for this user'
    $a = @('-m', 'cadfabric', 'configure')
    if ($Url) { $a += @('--url', $Url) }
    if ($Model) { $a += @('--model', $Model) }
    if ($Planner) { $a += @('--planner', $Planner) }
    if ($RelayHost) { $a += ('--relay-host=' + $RelayHost) }
    if ($RelayWorkdir) { $a += @('--relay-workdir', $RelayWorkdir) }
    if ($KeyFile) {
        $a += '--key-stdin'
        $key = (Get-Content -Raw -LiteralPath $KeyFile).Trim()
        $key | & $py @a
    } else { & $py @a }
    if ($LASTEXITCODE -ne 0) { throw 'cadfabric configure failed' }
}

Step 'Launchers'
$app = Join-Path $pydir 'Scripts\cadfabric-app.exe'
if (-not (Test-Path $app)) { throw 'cadfabric-app.exe was not created' }
[IO.File]::WriteAllText((Join-Path $Prefix 'cadfabric.cmd'), "@`"%~dp0python\python.exe`" -m cadfabric %*`r`n")
Copy-Item (Join-Path $here 'uninstall.ps1') $Prefix -Force
if (-not $NoShortcuts) {
    $sh = New-Object -ComObject WScript.Shell
    # one shortcut name per install: the default folder owns "CadFabric"; a custom -Prefix gets its own name so installs never overwrite each other
    $default = Join-Path $env:LOCALAPPDATA 'CadFabric'
    $name = if ($Prefix.TrimEnd('\') -ieq $default) { 'CadFabric' } else { 'CadFabric - ' + (Split-Path -Leaf $Prefix.TrimEnd('\')) }
    $targets = @((Join-Path ([Environment]::GetFolderPath('Programs')) ($name + '.lnk')))
    if (-not $NoDesktopShortcut) { $targets += (Join-Path ([Environment]::GetFolderPath('Desktop')) ($name + '.lnk')) }
    foreach ($t in $targets) {
        $l = $sh.CreateShortcut($t); $l.TargetPath = $app; $l.WorkingDirectory = $env:USERPROFILE; $l.Description = 'CadFabric - CAD design chat'; $l.Save()
        Write-Host ('shortcut: ' + $t)
    }
}

Step 'Self-check (CadQuery kernel + sandbox; the model URL is only checked if it is already configured)'
& $py -m cadfabric doctor
Write-Host ("`nInstalled. Start it from the Start Menu / Desktop shortcut 'CadFabric', or run: " + (Join-Path $Prefix 'cadfabric.cmd') + ' app') -ForegroundColor Green
Write-Host ('Uninstall: powershell -ExecutionPolicy Bypass -File "' + (Join-Path $Prefix 'uninstall.ps1') + '"')
