@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del v5_*.jsonl v5.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 8 --temp 0.3 --set held4 --tag v5 --out C:\wagent\v5_held4_imp.jsonl >> v5.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag v5 --out C:\wagent\v5_dev_imp.jsonl >> v5.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag v5 --out C:\wagent\v5_held_imp.jsonl >> v5.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --tag v5 --out C:\wagent\v5_held2_imp.jsonl >> v5.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held3 --tag v5 --out C:\wagent\v5_held3_imp.jsonl >> v5.log 2>&1
echo EXIT=done >> v5.log
