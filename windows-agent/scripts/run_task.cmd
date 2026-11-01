@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
rmdir /s /q sandbox 2>nul
del audit.jsonl run.log 2>nul
venv\Scripts\python -m wagent --url http://10.42.0.1:18090/v1 %* > run.log 2>&1
echo EXIT=%errorlevel% >> run.log
