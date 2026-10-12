@echo off
setlocal
cd /d "%~dp0"
title Fuel Reimbursement System

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found. Install Python 3.10+ from python.org and run this again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a virtual environment in .venv ...
  %PY% -m venv .venv || goto :fail
)

rem Bump the flag name whenever requirements.txt changes so packages get reinstalled.
if not exist ".venv\installed-v2.flag" (
  echo Installing packages, this happens only once ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail
  echo ok> ".venv\installed-v2.flag"
)

".venv\Scripts\python.exe" run.py %*
pause
exit /b 0

:fail
echo.
echo Setup failed, see the messages above.
pause
exit /b 1
