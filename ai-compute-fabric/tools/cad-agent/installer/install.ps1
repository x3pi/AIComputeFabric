<#
 AICF CAD Agent installer (per-user, no administrator rights needed).
 Installs: a private Python venv with solidworks-mcp (pinned, tested version), the agent scripts, config.json, the API key
 (file readable only by you), launchers, then runs doctor.py.

   powershell -ExecutionPolicy Bypass -File install.ps1 -GatewayUrl http://HOST:18080 [-ApiKey KEY] [-Model NAME]
                                                         [-InstallDir PATH] [-DataDir PATH] [-SkipDoctor]

 -GatewayUrl  base URL of the AICF gateway / LAN proxy (with or without /v1/chat/completions)
 -ApiKey      tenant key; if omitted you are asked (input hidden)
 Needs internet access to pypi.org (and github.com only when no Python 3.11+ is installed: uv is downloaded then).
 Re-running upgrades in place and keeps config.json and the key unless you pass new values.
#>
param(
  [Parameter(Mandatory = $true)][string]$GatewayUrl,
  [string]$ApiKey = "",
  [string]$Model = "Qwen3.8-27B-UD-Q4_K_M.gguf",
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "AICF-CAD\app"),
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA "AICF-CAD\data"),
  [switch]$SkipDoctor
)
$ErrorActionPreference = "Stop"
$McpVersion = "0.19.0"          # the version the agent was tested with; do not float
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Split-Path -Parent $here  # scripts live one level above installer\ in the package
if (-not (Test-Path (Join-Path $src "sw_agent.py"))) { $src = $here }   # flat layout also accepted
$log = Join-Path $env:TEMP ("aicf-cad-install-{0}.log" -f (Get-Date -Format yyyyMMdd-HHmmss))
Start-Transcript -Path $log | Out-Null

function Native {
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $rest = @(); if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }; & $args[0] @rest 2>&1 | ForEach-Object { "$_" } | Out-Host } finally { $ErrorActionPreference = $old }
  return $LASTEXITCODE
}
function Quiet {   # same, but output is discarded (probing)
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $rest = @(); if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }; & $args[0] @rest 2>&1 | Out-Null } finally { $ErrorActionPreference = $old }
  return $LASTEXITCODE
}
function Step($m) { Write-Host ("`n== " + $m) -ForegroundColor Cyan }
function Fail($m) { Write-Host ("FAILED: " + $m) -ForegroundColor Red; Stop-Transcript | Out-Null; Write-Host "Log: $log"; exit 1 }

Step "Checking this PC"
if ([Environment]::OSVersion.Version.Major -lt 10) { Fail "Windows 10 or newer is required" }
if ($PSVersionTable.PSVersion.Major -lt 5) { Fail "PowerShell 5.1 or newer is required" }
foreach ($f in "sw_agent.py","job_runner.py","advisor.py","aicfconf.py","doctor.py","ui_server.py","ui_page.html") {
  if (-not (Test-Path (Join-Path $src $f))) { Fail "package file missing: $f (run install.ps1 from the unpacked package)" }
}
$sw = $null
foreach ($k in "HKLM:\SOFTWARE\SolidWorks","HKLM:\SOFTWARE\WOW6432Node\SolidWorks") {
  if (Test-Path $k) { Get-ChildItem $k -ErrorAction SilentlyContinue | ForEach-Object {
      $p = Join-Path $_.PSPath "Setup"; if (Test-Path $p) { $d = (Get-ItemProperty $p -ErrorAction SilentlyContinue)."SolidWorks Folder"; if ($d -and (Test-Path (Join-Path $d "SLDWORKS.exe"))) { $sw = Join-Path $d "SLDWORKS.exe" } } } }
}
if ($sw) { Write-Host "SolidWorks found: $sw" } else { Write-Host "WARNING: SolidWorks not found. The agent installs anyway; set sw_exe in config.json if it lives elsewhere." -ForegroundColor Yellow }

$chatUrl = $GatewayUrl.TrimEnd("/")
if ($chatUrl -notmatch "/chat/completions$") { $chatUrl = $chatUrl -replace "/v1$","" ; $chatUrl = $chatUrl + "/v1/chat/completions" }
if ($ApiKey -eq "") {
  $existing = Join-Path $InstallDir "api.key"
  if (Test-Path $existing) { Write-Host "Keeping the existing API key (pass -ApiKey to replace it)." }
  else { $sec = Read-Host "API key (hidden)" -AsSecureString; $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)) }
}

Step "Creating folders"
New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir | Out-Null

Step "Python environment"
$venv = Join-Path $InstallDir "venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
  $made = $false
  foreach ($cand in @(@("py","-3.12"),@("py","-3.11"),@("py","-3.13"))) {
    if (-not $made -and (Get-Command $cand[0] -ErrorAction SilentlyContinue)) {
      if ((Quiet $cand[0] $cand[1] "-c" "import sys") -eq 0) {
        if ((Native $cand[0] $cand[1] "-m" "venv" $venv) -eq 0) { $made = $true; Write-Host ("venv from " + ($cand -join " ")) }
      }
    }
  }
  if (-not $made) {
    Write-Host "No Python 3.11+ found; using uv (downloaded from github.com/astral-sh/uv)."
    $uvDir = Join-Path $InstallDir "tools"; New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
    $zip = Join-Path $env:TEMP "uv-win.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
    Expand-Archive -Force -Path $zip -DestinationPath $uvDir
    $uv = Get-ChildItem -Path $uvDir -Recurse -Filter uv.exe | Select-Object -First 1
    if (-not $uv) { Fail "uv.exe not found after download" }
    if ((Native $uv.FullName "venv" "--python" "3.12" $venv) -ne 0) { Fail "uv could not create the Python environment" }
  }
}
if (-not (Test-Path $py)) { Fail "Python environment was not created" }

Step "Installing solidworks-mcp $McpVersion"
$rc = Native $py "-m" "pip" "install" "--disable-pip-version-check" "--quiet" "solidworks-mcp==$McpVersion"
if ($rc -ne 0) {
  $uvExe = Get-ChildItem -Path (Join-Path $InstallDir "tools") -Recurse -Filter uv.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($uvExe) { if ((Native $uvExe.FullName "pip" "install" "--python" $py "solidworks-mcp==$McpVersion") -ne 0) { Fail "pip/uv could not install solidworks-mcp" } }
  else { Fail "pip could not install solidworks-mcp $McpVersion (no internet access to pypi.org?)" }
}
$mcp = Join-Path $venv "Scripts\solidworks-mcp.exe"
if (-not (Test-Path $mcp)) { Fail "solidworks-mcp.exe missing after install" }

Step "Copying the agent"
foreach ($f in "sw_agent.py","job_runner.py","advisor.py","aicfconf.py","doctor.py","ui_server.py","ui_page.html","canary_test.py","test_aicfconf.py") {
  if (Test-Path (Join-Path $src $f)) { Copy-Item -Force (Join-Path $src $f) $InstallDir }
}
if (Test-Path (Join-Path $src "jobs")) { Copy-Item -Recurse -Force (Join-Path $src "jobs") $InstallDir }

Step "Writing config and key"
$keyFile = Join-Path $InstallDir "api.key"
if ($ApiKey -ne "") {
  [IO.File]::WriteAllText($keyFile, $ApiKey.Trim(), (New-Object Text.UTF8Encoding($false)))
}
if (Test-Path $keyFile) {
  # only this user can read the key
  if ((Quiet "icacls" $keyFile "/inheritance:r" "/grant:r" ("{0}:(R,W)" -f $env:USERNAME)) -ne 0) { Write-Host "WARNING: could not restrict permissions on the key file" -ForegroundColor Yellow }
}
$cfgPath = Join-Path $InstallDir "config.json"
$cfg = [ordered]@{ url = $chatUrl; model = $Model; key_file = "api.key"; mcp_exe = $mcp; data_dir = $DataDir }
if ($sw) { $cfg["sw_exe"] = $sw }
[IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))

Step "Launchers"
$l1 = '@echo off' + "`r`n" + '"' + $py + '" "' + (Join-Path $InstallDir "sw_agent.py") + '" %*' + "`r`n"
$l2 = '@echo off' + "`r`n" + '"' + $py + '" -X utf8 "' + (Join-Path $InstallDir "job_runner.py") + '" %*' + "`r`n"
$l4 = '@echo off' + "`r`n" + '"' + $py + '" -X utf8 "' + (Join-Path $InstallDir "ui_server.py") + '" %*' + "`r`n" + 'pause' + "`r`n"
$l3 = '@echo off' + "`r`n" + '"' + $py + '" -X utf8 "' + (Join-Path $InstallDir "doctor.py") + '" %*' + "`r`n" + 'pause' + "`r`n"
Set-Content -Path (Join-Path $InstallDir "aicf-cad-chat.cmd") -Value $l1 -Encoding ASCII
Set-Content -Path (Join-Path $InstallDir "aicf-cad-job.cmd") -Value $l2 -Encoding ASCII
Set-Content -Path (Join-Path $InstallDir "aicf-cad-ui.cmd") -Value $l4 -Encoding ASCII
Set-Content -Path (Join-Path $InstallDir "aicf-cad-doctor.cmd") -Value $l3 -Encoding ASCII

if (-not $SkipDoctor) {
  Step "Self-check (doctor)"
  $docExit = Native $py "-X" "utf8" (Join-Path $InstallDir "doctor.py")
} else { $docExit = 0 }

Stop-Transcript | Out-Null
Write-Host "`nInstalled to $InstallDir (data: $DataDir). Install log: $log"
Write-Host "Run:   $InstallDir\aicf-cad-ui.cmd          (chat in Chrome, recommended)"
Write-Host "       $InstallDir\aicf-cad-chat.cmd        (text console chat)"
Write-Host "       $InstallDir\aicf-cad-job.cmd job.json (unattended job)"
Write-Host "       $InstallDir\aicf-cad-doctor.cmd       (re-check)"
if ($docExit -ne 0) { Write-Host "The self-check reported FAIL lines above: fix them before use." -ForegroundColor Red; exit 2 }
