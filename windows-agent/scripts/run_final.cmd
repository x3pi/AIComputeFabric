@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del fin_*.jsonl fin.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held3 --baseline --tag base --out C:\wagent\fin_held3_base.jsonl >> fin.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held3 --tag improved3 --out C:\wagent\fin_held3_imp.jsonl >> fin.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --tag improved3 --out C:\wagent\fin_held2_imp.jsonl >> fin.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag improved3 --out C:\wagent\fin_held_imp.jsonl >> fin.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag improved3 --out C:\wagent\fin_dev_imp.jsonl >> fin.log 2>&1
echo EXIT=done >> fin.log
