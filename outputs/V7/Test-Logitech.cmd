@echo off
setlocal
cd /d "%~dp0"
set "LOCAL_PYTHON=%~dp0..\..\work\preview-env\Scripts\python.exe"
if exist "%LOCAL_PYTHON%" (
    "%LOCAL_PYTHON%" "%~dp0dll_desktop_test.py"
) else (
    "%~dp0.venv\Scripts\python.exe" "%~dp0dll_desktop_test.py"
)
if errorlevel 1 pause
