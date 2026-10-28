<#
 AICF worker-guard as a Windows service.
 STATUS: NOT RUN ON REAL WINDOWS YET. Written from the sc.exe documentation; expect to
 fix details on the first real run, and report them in plan section I.4.
 Run from an elevated PowerShell.   Example:
   .\install-service.ps1 -Bin C:\aicf\worker-guard.exe -LlamaServer C:\aicf\llama\llama-server.exe `
       -Model C:\aicf\models\Qwen3-8B-Q4_K_M.gguf -Gateway 10.0.0.2:7443 -AdvertiseHost 10.0.0.15
#>
param(
  [Parameter(Mandatory)] [string] $Bin,
  [Parameter(Mandatory)] [string] $LlamaServer,
  [Parameter(Mandatory)] [string] $Model,
  [string] $Gateway = "",
  [string] $AdvertiseHost = "127.0.0.1",
  [string] $EngineHost = "",
  [string] $Listen = "",
  [string] $WorkerId = "",
  [string] $ModelsDir = "",
  [string] $AdaptersDir = "",
  [string] $TlsCA = "",
  [string] $TlsCert = "",
  [string] $TlsKey = "",
  [string] $TlsServerName = "",
  [string] $EngineLog = "",
  [string] $StateFile = "",
  [int] $Parallel = 0,
  [int] $Ctx = 0,
  [string] $Name = "AICFWorkerGuard",
  [int] $HeadroomMiB = 1536
)
$ErrorActionPreference = "Stop"
$svcArgs = "--llama-server `"$LlamaServer`" --model `"$Model`" --headroom-mib $HeadroomMiB --advertise-host $AdvertiseHost"
if ($Gateway) { $svcArgs += " --gateway $Gateway" }
if ($EngineHost) { $svcArgs += " --engine-host `"$EngineHost`"" }
if ($Listen) { $svcArgs += " --listen `"$Listen`"" }
if ($WorkerId) { $svcArgs += " --worker-id `"$WorkerId`"" }
if ($ModelsDir) { $svcArgs += " --models-dir `"$ModelsDir`"" }
if ($AdaptersDir) { $svcArgs += " --adapters-dir `"$AdaptersDir`"" }
if ($TlsCA) { $svcArgs += " --tls-ca `"$TlsCA`"" }
if ($TlsCert) { $svcArgs += " --tls-cert `"$TlsCert`"" }
if ($TlsKey) { $svcArgs += " --tls-key `"$TlsKey`"" }
if ($TlsServerName) { $svcArgs += " --tls-server-name `"$TlsServerName`"" }
if ($EngineLog) { $svcArgs += " --engine-log `"$EngineLog`"" }
if ($StateFile) { $svcArgs += " --state-file `"$StateFile`"" }
if ($Parallel -gt 0) { $svcArgs += " --parallel $Parallel" }
if ($Ctx -gt 0) { $svcArgs += " --ctx $Ctx" }
if (Get-Service $Name -ErrorAction SilentlyContinue) { sc.exe stop $Name | Out-Null; sc.exe delete $Name | Out-Null; Start-Sleep 2 }
sc.exe create $Name binPath= "`"$Bin`" $svcArgs" start= delayed-auto DisplayName= "AICF Worker Guard" | Out-Null
# restart after a crash (plan D13: roll back if it crashes more than 3 times in 5 minutes is a later item)
sc.exe failure $Name reset= 300 actions= restart/5000/restart/5000/restart/30000 | Out-Null
sc.exe start $Name
Get-Service $Name
