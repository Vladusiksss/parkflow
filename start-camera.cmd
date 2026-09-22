@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-camera.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" run.py %*
goto end
:failed
echo Setup failed. Check Python and your internet connection.
:end
pause
