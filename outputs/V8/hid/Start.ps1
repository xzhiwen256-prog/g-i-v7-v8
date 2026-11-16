param([switch]$Verify,[switch]$MouseOnly)
$ErrorActionPreference = 'Stop'
if (!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argText = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    if ($Verify) { $argText += ' -Verify' }
    if ($MouseOnly) { $argText += ' -MouseOnly' }
    Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $argText
    exit
}
$env:DOTNET_ROOT = Join-Path $PSScriptRoot 'runtime'
$mode = if ($Verify) { 'verify' } elseif ($MouseOnly) { 'host-mouse' } else { 'host' }
& (Join-Path $PSScriptRoot 'runtime\dotnet.exe') (Join-Path $PSScriptRoot 'app\VirtualKM.dll') $mode
