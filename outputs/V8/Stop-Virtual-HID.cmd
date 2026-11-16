@echo off
setlocal
cd /d "%~dp0"
echo First close the game and preview windows, then press a key to stop Virtual HID.
pause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0hid\Control.ps1" stop
if errorlevel 1 pause
