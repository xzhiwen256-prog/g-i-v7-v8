[CmdletBinding()]
param(
    [Parameter()]
    [string]$Root = $PSScriptRoot,

    [Parameter()]
    [switch]$RequireCleanOutputs
)

$ErrorActionPreference = 'Stop'
$rootItem = Get-Item -LiteralPath $Root -Force
if (-not ($rootItem -is [System.IO.DirectoryInfo])) {
    throw 'The root must be an extracted archive or Git checkout directory.'
}

$manifest = Join-Path $rootItem.FullName 'FILE_MANIFEST.json'
$records = @(Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json)
if ($records.Count -eq 0) { throw 'The archive manifest is empty.' }

$paths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$checkedDirs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$counts = @{ V7 = 0; V8 = 0 }
$problems = [System.Collections.Generic.List[string]]::new()

foreach ($record in $records) {
    $relative = $record.path
    if ($relative -isnot [string] -or
        $relative -cnotmatch '^outputs/(V7|V8)/[^\\:*?"<>|\x00-\x1F]+\z' -or
        $record.sha256 -notmatch '^[0-9a-f]{64}\z' -or
        $record.bytes -notmatch '^[0-9]+\z') {
        throw 'Invalid path, size or SHA-256 in FILE_MANIFEST.json.'
    }
    $parts = $relative.Split('/')
    foreach ($part in $parts) {
        if (-not $part -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw 'Invalid path component in FILE_MANIFEST.json.'
        }
    }
    if (-not $paths.Add($relative)) { throw "Duplicate manifest path: $relative" }
    $counts[$parts[1]]++

    $directory = $rootItem.FullName
    for ($i = 0; $i -lt ($parts.Length - 1); $i++) {
        $directory = Join-Path $directory $parts[$i]
        if ($checkedDirs.Contains($directory)) { continue }
        $item = Get-Item -LiteralPath $directory -Force -ErrorAction SilentlyContinue
        if (-not $item) { break }
        if (-not ($item -is [System.IO.DirectoryInfo]) -or
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Invalid or redirected directory for manifest path: $relative"
        }
        [void]$checkedDirs.Add($directory)
    }

    $path = Join-Path $rootItem.FullName $relative
    $file = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not ($file -is [System.IO.FileInfo])) {
        $problems.Add("Missing file: $relative")
    } elseif ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        $problems.Add("Redirected file: $relative")
    } elseif ($file.Length -ne [long]$record.bytes) {
        $problems.Add("Size differs: $relative")
    } elseif ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $record.sha256) {
        $problems.Add("SHA-256 differs: $relative")
    }
}

if (-not $counts.V7 -or -not $counts.V8) { throw 'Both V7 and V8 must be in the manifest.' }

if ($RequireCleanOutputs -and $problems.Count -eq 0) {
    $outputRoot = Join-Path $rootItem.FullName 'outputs'
    foreach ($file in Get-ChildItem -LiteralPath $outputRoot -File -Recurse -Force) {
        $relative = $file.FullName.Substring($rootItem.FullName.TrimEnd('\','/').Length + 1).Replace('\','/')
        if (-not $paths.Contains($relative)) { $problems.Add("Extra file: $relative") }
    }
}

$problems | Select-Object -First 15 | ForEach-Object { Write-Warning $_ }
if ($problems.Count) { throw "$($problems.Count) archive check(s) failed." }
Write-Output "ARCHIVE OK: $($records.Count) files verified by size and SHA-256 (V7: $($counts.V7), V8: $($counts.V8))."
Write-Output 'Scope: downloaded archive only. No software, models, capture, drivers or devices were run.'
