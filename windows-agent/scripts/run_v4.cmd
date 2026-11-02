@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del v4_*.jsonl v4.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 8 --temp 0.3 --set held4 --baseline --tag base --out C:\wagent\v4_held4_base.jsonl >> v4.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 8 --temp 0.3 --set held4 --tag v4 --out C:\wagent\v4_held4_imp.jsonl >> v4.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag v4 --out C:\wagent\v4_dev_imp.jsonl >> v4.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag v4 --out C:\wagent\v4_held_imp.jsonl >> v4.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --tag v4 --out C:\wagent\v4_held2_imp.jsonl >> v4.log 2>&1
echo EXIT=done >> v4.log
