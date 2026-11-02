@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del v8_*.jsonl v8.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 8 --temp 0.3 --set held4 --tag v8 --out C:\wagent\v8_held4_imp.jsonl >> v8.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag v8 --out C:\wagent\v8_dev_imp.jsonl >> v8.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag v8 --out C:\wagent\v8_held_imp.jsonl >> v8.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --tag v8 --out C:\wagent\v8_held2_imp.jsonl >> v8.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held3 --tag v8 --out C:\wagent\v8_held3_imp.jsonl >> v8.log 2>&1
echo EXIT=done >> v8.log
