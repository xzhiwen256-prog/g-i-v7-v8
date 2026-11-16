@echo off
setlocal
cd /d "%~dp0"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
    "%BUNDLED_PYTHON%" -m venv .venv
) else (
    py -3.12 -m venv .venv
)
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-preview.txt
if errorlevel 1 goto failed
echo Ready. Open Start-Preview.cmd to launch.
pause
exit /b 0
:failed
echo Setup failed. Python 3.12 and internet access are required.
pause
exit /b 1
