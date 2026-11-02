@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
set URL=http://10.42.0.1:18090/v1
del ab_*.jsonl ab.log bench.lock 2>nul
for %%S in (held dev) do (
  venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set %%S --baseline --tag base --out C:\wagent\ab_%%S_base.jsonl >> ab.log 2>&1
  venv\Scripts\python -u -m wagent.bench --url %URL% --n 5 --temp 0.3 --set %%S --tag improved --out C:\wagent\ab_%%S_imp.jsonl >> ab.log 2>&1
)
echo EXIT=done >> ab.log
