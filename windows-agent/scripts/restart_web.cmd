@echo off
rem Tat server Wagent cu (chi pythonw chay wagent --web) roi mo lai bang launcher; chay trong phien desktop (schtasks /IT).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*wagent*--web*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
taskkill /F /IM notepad.exe >nul 2>&1
timeout /t 2 /nobreak >nul
C:\wagent\venv\Scripts\pythonw.exe C:\wagent\scripts\launch.pyw
