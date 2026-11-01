@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
venv\Scripts\python -u -m wagent --web 8765 > web.log 2>&1
