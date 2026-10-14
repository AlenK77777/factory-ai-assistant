# Load every workbook in inbox/ into PostgreSQL.
# Orders are loaded before defects. Locked files stay in inbox.
# Prints one summary line. Exit 0 = ok, 1 = some rows rejected, 2 = failed.
# Power Automate Desktop can run this file with no arguments.

param(
    [string]$ProjectRoot = "",
    [string]$Python = "",
    [string]$Loader = ""
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if (-not $Python) {
    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not $Loader) {
    $Loader = Join-Path $ProjectRoot "ingest\load_excel.py"
}

$Inbox = Join-Path $ProjectRoot "inbox"
$Processed = Join-Path $ProjectRoot "processed"
$Failed = Join-Path $ProjectRoot "failed"

function Test-InboxFileLocked([System.IO.FileInfo]$File) {
    $companion = Join-Path $File.DirectoryName ("~$" + $File.Name)
    if (Test-Path -LiteralPath $companion) {
        return $true
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $File.FullName,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::None
        )
        return $false
    } catch {
        return $true
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Format-ProcessArgument([string]$Text) {
    if ($Text -match '[\s"]') {
        return '"' + ($Text -replace '"', '\"') + '"'
    }
    return $Text
}

function Invoke-Loader {
    param([string[]]$Arguments)
    $quoted = ($Arguments | ForEach-Object { Format-ProcessArgument $_ }) -join " "
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $info.Arguments = $quoted
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $info
    [void]$proc.Start()
    $text = $proc.StandardOutput.ReadToEnd()
    $null = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    return @{ Code = $proc.ExitCode; Text = $text.Trim() }
}

function Write-InboxLog {
    param([string]$FileName, [string]$Result, [int]$Loaded, [int]$Rejected)
    Invoke-Loader -Arguments @($Loader, "--inbox-log", $FileName, $Result, "$Loaded", "$Rejected") | Out-Null
}

function Move-InboxFile {
    param([System.IO.FileInfo]$File, [string]$DestinationDir, [string]$CsvPath)
    $stamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
    $target = Join-Path $DestinationDir ($File.BaseName + "_" + $stamp + $File.Extension)
    $suffix = 1
    while (Test-Path -LiteralPath $target) {
        $target = Join-Path $DestinationDir ($File.BaseName + "_" + $stamp + "_" + $suffix + $File.Extension)
        $suffix += 1
    }
    Move-Item -LiteralPath $File.FullName -Destination $target
    if ($CsvPath -and (Test-Path -LiteralPath $CsvPath)) {
        $csvTarget = Join-Path $DestinationDir ($File.BaseName + "_" + $stamp + "_rejected.csv")
        Move-Item -LiteralPath $CsvPath -Destination $csvTarget
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Output "Python was not found. Nothing was moved."
    exit 2
}

New-Item -ItemType Directory -Force -Path $Inbox, $Processed, $Failed | Out-Null

$check = Invoke-Loader -Arguments @($Loader, "--check")
if ($check.Code -ne 0) {
    if ($check.Text) {
        Write-Output $check.Text
    } else {
        Write-Output "Database is not reachable. Nothing was moved."
    }
    Write-InboxLog -FileName "database" -Result "failed" -Loaded 0 -Rejected 0
    exit 2
}

$files = @(Get-ChildItem -LiteralPath $Inbox -Filter "*.xlsx" -File | Where-Object { -not $_.Name.StartsWith("~`$") })
if ($files.Count -eq 0) {
    Write-Output "No files in inbox."
    exit 0
}

$plan = @()
foreach ($file in $files) {
    if (Test-InboxFileLocked $file) {
        $plan += [pscustomobject]@{ File = $file; Kind = "locked"; Rank = 2 }
        continue
    }
    $detected = Invoke-Loader -Arguments @($Loader, "--detect", $file.FullName)
    $kind = $detected.Text
    if ($kind -eq "orders") {
        $plan += [pscustomobject]@{ File = $file; Kind = "orders"; Rank = 0 }
    } elseif ($kind -eq "defects") {
        $plan += [pscustomobject]@{ File = $file; Kind = "defects"; Rank = 1 }
    } else {
        $plan += [pscustomobject]@{ File = $file; Kind = "unknown"; Rank = 2 }
    }
}

$plan = @($plan | Sort-Object Rank, @{ Expression = { $_.File.Name } })
$parts = @()
$exitCode = 0

foreach ($item in $plan) {
    $name = $item.File.Name
    if ($item.Kind -eq "locked") {
        $parts += ($name + ": locked")
        Write-InboxLog -FileName $name -Result "locked" -Loaded 0 -Rejected 0
        continue
    }
    if ($item.Kind -eq "unknown") {
        Move-InboxFile -File $item.File -DestinationDir $Failed -CsvPath ""
        $parts += ($name + ": failed")
        Write-InboxLog -FileName $name -Result "failed" -Loaded 0 -Rejected 0
        $exitCode = 2
        continue
    }

    $tempCsv = Join-Path $env:TEMP ("inbox-rejected-" + [guid]::NewGuid().ToString() + ".csv")
    $loaded = Invoke-Loader -Arguments @($Loader, "--file", $item.File.FullName, "--rejected-csv", $tempCsv)
    if ($loaded.Code -eq 2 -or $loaded.Code -eq $null) {
        Move-InboxFile -File $item.File -DestinationDir $Failed -CsvPath $tempCsv
        $parts += ($name + ": failed")
        Write-InboxLog -FileName $name -Result "failed" -Loaded 0 -Rejected 0
        $exitCode = 2
        continue
    }

    Move-InboxFile -File $item.File -DestinationDir $Processed -CsvPath $tempCsv
    if ($loaded.Text) {
        $parts += $loaded.Text
    } else {
        $parts += ($name + ": loaded")
    }
    $rejected = 0
    if ($loaded.Text -match "(\d+) rejected") {
        $rejected = [int]$Matches[1]
    }
    $loadedCount = 0
    if ($loaded.Text -match "(\d+) loaded") {
        $loadedCount = [int]$Matches[1]
    }
    $result = "loaded"
    if ($loaded.Code -eq 1 -or $rejected -gt 0) {
        $result = "rejected"
        if ($exitCode -eq 0) {
            $exitCode = 1
        }
    }
    Write-InboxLog -FileName $name -Result $result -Loaded $loadedCount -Rejected $rejected
}

Write-Output ($parts -join "; ")
exit $exitCode
