@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del v6_*.jsonl v6.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 8 --temp 0.3 --set held4 --tag v6 --out C:\wagent\v6_held4_imp.jsonl >> v6.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag v6 --out C:\wagent\v6_dev_imp.jsonl >> v6.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag v6 --out C:\wagent\v6_held_imp.jsonl >> v6.log 2>&1
echo EXIT=done >> v6.log
