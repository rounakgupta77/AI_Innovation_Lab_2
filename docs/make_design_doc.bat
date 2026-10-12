@echo off
rem Builds docs\Technical_Design_Document.docx (and refreshes the block diagram slide picture).
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first, it sets up Python for this project.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import docx, pptx" >nul 2>nul || (
  echo Installing python-docx and python-pptx, this happens only once ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check python-docx python-pptx || goto :fail
)

set "TRACK="
set /p TRACK=Which track did you select? (press Enter to leave a placeholder):

echo Building the block diagram slide ...
".venv\Scripts\python.exe" docs\make_block_diagram.py --preview || echo (Slide picture skipped: PowerPoint is needed for it. The document will point to the .pptx instead.)

echo Building the Word document ...
".venv\Scripts\python.exe" docs\make_design_doc.py --track "%TRACK%" || goto :fail
start "" "%~dp0Technical_Design_Document.docx"
pause
exit /b 0

:fail
echo.
echo Something went wrong, see the messages above.
pause
exit /b 1
