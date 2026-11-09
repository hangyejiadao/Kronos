@echo off
cd /d D:\Kronos\webui
set "MT5_DEMO_PROXY=http://127.0.0.1:7890"
set "NO_PROXY=127.0.0.1,localhost"
D:\Kronos\.venv-gold\Scripts\python.exe -u app.py > webui.log 2>&1
