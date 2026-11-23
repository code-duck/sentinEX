$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'logs'
$pidFile = Join-Path $logDirectory 'sentinex.pid.json'

function Show-StopMessage([string]$message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        'SentinEX',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
}

if (-not (Test-Path -LiteralPath $pidFile)) {
    Show-StopMessage 'SentinEX is not running (or it was started from another terminal).'
    exit 0
}

try {
    $state = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $processId = [int]$state.ProcessId
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if (-not $processInfo) {
        Remove-Item -LiteralPath $pidFile -Force
        Show-StopMessage 'SentinEX was already stopped.'
        exit 0
    }

    $expectedPython = [IO.Path]::GetFullPath($pythonPath)
    $actualPython = [IO.Path]::GetFullPath([string]$processInfo.ExecutablePath)
    $commandLine = [string]$processInfo.CommandLine
    if ($actualPython -ine $expectedPython -or
        $commandLine -notmatch '(?i)-m\s+streamlit\s+run\s+app\.py' -or
        $commandLine -notmatch [regex]::Escape($projectRoot)) {
        Remove-Item -LiteralPath $pidFile -Force
        Show-StopMessage 'The saved process ID is not a SentinEX server. No process was stopped.'
        exit 1
    }

    Stop-Process -Id $processId -Force
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Show-StopMessage "SentinEX has stopped.`n`nRecent logs are in:`n$logDirectory"
} catch {
    Show-StopMessage "SentinEX could not be stopped safely:`n$($_.Exception.Message)`n`nNo unrelated process was intentionally stopped."
    exit 1
}
