@echo off
setlocal
cd /d "%~dp0"
set "PREVIEW_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PREVIEW_PYTHON%" if exist "%~dp0python-cpu-path.txt" set /p PREVIEW_PYTHON=<"%~dp0python-cpu-path.txt"
if not exist "%PREVIEW_PYTHON%" if exist "%~dp0python-path.txt" set /p PREVIEW_PYTHON=<"%~dp0python-path.txt"
if not exist "%PREVIEW_PYTHON%" (
  echo Please run Setup-Preview.cmd first.
  pause
  exit /b 1
)
"%PREVIEW_PYTHON%" "%~dp0launch_windows_admin.py" --cpu
if errorlevel 1 pause
