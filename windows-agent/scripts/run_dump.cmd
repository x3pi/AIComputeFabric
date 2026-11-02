@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
venv\Scripts\python scripts\dump_calc.py > dump.log 2>&1
echo EXIT=done >> dump.log
