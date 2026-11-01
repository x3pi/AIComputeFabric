@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
del bench.jsonl 2>nul
venv\Scripts\python -u -m wagent.bench --url http://10.42.0.1:18090/v1 %* > bench.log 2>&1
echo EXIT=%errorlevel% >> bench.log
