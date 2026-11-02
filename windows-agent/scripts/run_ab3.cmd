@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
del ab3_*.jsonl ab3.log bench.lock 2>nul
venv\Scripts\python -u -m wagent.bench --url http://10.42.0.1:18090/v1 --n 5 --temp 0.3 --set dev --tag improved2 --out C:\wagent\ab3_dev_imp.jsonl >> ab3.log 2>&1
echo EXIT=done >> ab3.log
