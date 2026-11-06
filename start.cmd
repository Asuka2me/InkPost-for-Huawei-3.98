@echo off
cd /d "%~dp0"
python -c "import PIL" >nul 2>&1
if errorlevel 1 (
  echo Please install Pillow: python -m pip install Pillow
  pause
  exit /b 1
)
start "" http://127.0.0.1:8765
python ink_image_server.py
pause
