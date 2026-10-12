@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first, it sets up Python for this project.
  pause
  exit /b 1
)
echo Creating demo bills ...
".venv\Scripts\python.exe" demo\make_demo_bills.py
start "" "%~dp0bills"
pause
