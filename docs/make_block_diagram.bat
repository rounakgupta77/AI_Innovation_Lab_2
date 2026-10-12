@echo off
rem Builds docs\Reimbursement_Block_Diagram.pptx (and a PNG preview if PowerPoint is installed).
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first, it sets up Python for this project.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pptx" >nul 2>nul || (
  echo Installing python-pptx, this happens only once ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check python-pptx || goto :fail
)
".venv\Scripts\python.exe" docs\make_block_diagram.py --preview || goto :fail
start "" "%~dp0Reimbursement_Block_Diagram.pptx"
pause
exit /b 0

:fail
echo.
echo Something went wrong, see the messages above.
pause
exit /b 1
