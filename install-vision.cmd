@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-camera.txt "ultralytics>=8.3,<9"
if errorlevel 1 goto failed
if not exist "models" mkdir models
cd models
"..\.venv\Scripts\python.exe" -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"
if errorlevel 1 goto failed
echo Vision module installed. Restart ParkFlow.
goto end
:failed
echo Installation failed. Check Python and your internet connection.
:end
pause
