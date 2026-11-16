@echo off
setlocal
cd /d "%~dp0"
set "LOCAL_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%LOCAL_PYTHON%" if exist "%~dp0python-path.txt" set /p LOCAL_PYTHON=<"%~dp0python-path.txt"
if exist "%LOCAL_PYTHON%" goto run_local
if exist "%~dp0.venv\Scripts\python.exe" goto run_portable
echo Please run Setup-Preview.cmd first.
pause
exit /b 1
:run_local
"%LOCAL_PYTHON%" "%~dp0launch_windows_admin.py"
goto finished
:run_portable
"%~dp0.venv\Scripts\python.exe" "%~dp0launch_windows_admin.py"
:finished
if errorlevel 1 pause
