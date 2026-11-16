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
".venv\Scripts\python.exe" -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-preview.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import torch; print(torch.__version__); print('CUDA:', torch.cuda.is_available()); assert torch.cuda.is_available(), 'CUDA unavailable; check the NVIDIA driver'"
if errorlevel 1 goto failed
echo Ready. Open Start-Target-Follow.cmd to launch with automatic GPU selection.
pause
exit /b 0
:failed
echo GPU setup failed. Python 3.12, NVIDIA GPU and compatible driver are required.
pause
exit /b 1
