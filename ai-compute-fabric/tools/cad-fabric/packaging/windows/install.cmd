@echo off
rem Double-click to install CadFabric for the current user (no administrator rights needed). Extra options are passed on, e.g.
rem   install.cmd -Url http://192.168.1.40:18080/v1/chat/completions -Model Qwen3.8-27B-UD-Q4_K_M.gguf -KeyFile C:\path\key.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
