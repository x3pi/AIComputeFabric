@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del ab2_*.jsonl ab2.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --baseline --tag base --out C:\wagent\ab2_held2_base.jsonl >> ab2.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held2 --tag improved2 --out C:\wagent\ab2_held2_imp.jsonl >> ab2.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set held --tag improved2 --out C:\wagent\ab2_held_imp.jsonl >> ab2.log 2>&1
venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set dev --tag improved2 --out C:\wagent\ab2_dev_imp.jsonl >> ab2.log 2>&1
echo EXIT=done >> ab2.log
