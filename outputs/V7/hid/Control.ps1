param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)
$ErrorActionPreference = 'Stop'
$env:DOTNET_ROOT = Join-Path $PSScriptRoot 'runtime'
& (Join-Path $PSScriptRoot 'runtime\dotnet.exe') (Join-Path $PSScriptRoot 'app\VirtualKM.dll') @CommandArgs
exit $LASTEXITCODE
