@echo off
setlocal
set "DOTNET_ROOT=%~dp0runtime"
"%~dp0runtime\dotnet.exe" "%~dp0app\VirtualKM.dll" stream %*
exit /b %errorlevel%
