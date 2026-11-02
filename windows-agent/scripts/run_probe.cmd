@echo off
cd /d C:\wagent
set PYTHONPATH=C:\wagent\src
C:\wagent\venv\Scripts\python scripts\probe_notepad.py > probe.log 2>&1
taskkill /F /IM notepad.exe >nul 2>&1
echo EXIT=done >> probe.log
