@echo off
cd /d "%~dp0"
"%LocalAppData%\Programs\Python\Python313\python.exe" main.py >> "%~dp0bot_run.log" 2>&1
