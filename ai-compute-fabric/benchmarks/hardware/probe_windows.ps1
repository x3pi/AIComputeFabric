<#
 Windows GPU probe: answers the one question that decides the worker design on Windows
 (plan I.4 / risk R-WDDM): can we see how much VRAM a given process holds, and how fast does
 it return when a process dies?   STATUS: NOT RUN ON REAL WINDOWS YET.
 Usage (PowerShell):  .\probe_windows.ps1 -Out probe_windows.json
#>
param([string] $Out = "probe_windows.json")
$ErrorActionPreference = "Continue"
$smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
if (-not $smi) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
$r = [ordered]@{ schema_version = 1; os = (Get-CimInstance Win32_OperatingSystem).Caption; time = (Get-Date).ToString("o") }
$r.gpu = & $smi --query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu,driver_model.current --format=csv,noheader 2>&1
# 1. what nvidia-smi says per process (WDDM prints [N/A])
$r.smi_compute_apps = & $smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>&1
# 2. what Windows performance counters say per process and per adapter
foreach ($c in @("\GPU Process Memory(*)\Dedicated Usage", "\GPU Adapter Memory(*)\Dedicated Usage")) {
  try {
    $s = (Get-Counter $c -ErrorAction Stop).CounterSamples | Where-Object { $_.CookedValue -gt 0 } |
         Sort-Object CookedValue -Descending | Select-Object -First 8 InstanceName, CookedValue
    $r[$c] = $s
  } catch { $r[$c] = "counter unavailable: $($_.Exception.Message)" }
}
$r.cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfCores, NumberOfLogicalProcessors)
$r.ram_gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
$r.ram_modules = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, Speed, ConfiguredClockSpeed)
$r | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $Out
Write-Host "wrote $Out - send it back together with the output of reclaim_poc.py"
