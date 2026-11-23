$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'logs'
$pidFile = Join-Path $logDirectory 'sentinex.pid.json'
$healthUrl = 'http://127.0.0.1:8501/_stcore/health'
$appUrl = 'http://127.0.0.1:8501'

function Show-LaunchMessage([string]$message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        'SentinEX',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
}

function Get-VerifiedSentinEXProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    try {
        $state = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$state.ProcessId)"
        if (-not $processInfo) { return $null }
        $expectedPython = [IO.Path]::GetFullPath($pythonPath)
        $actualPython = [IO.Path]::GetFullPath([string]$processInfo.ExecutablePath)
        $commandLine = [string]$processInfo.CommandLine
        if ($actualPython -ine $expectedPython -or
            $commandLine -notmatch '(?i)-m\s+streamlit\s+run\s+app\.py' -or
            $commandLine -notmatch [regex]::Escape($projectRoot)) {
            return $null
        }
        return Get-Process -Id ([int]$state.ProcessId) -ErrorAction Stop
    } catch {
        return $null
    }
}

function Test-SentinEXHealth {
    try {
        return (Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2) -eq 'ok'
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Show-LaunchMessage "SentinEX's Python environment was not found:`n$pythonPath"
    exit 1
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$existingProcess = Get-VerifiedSentinEXProcess
if ($existingProcess) {
    if (Test-SentinEXHealth) {
        Start-Process $appUrl
        exit 0
    }
    Show-LaunchMessage "SentinEX is already starting. Wait a few seconds, then open $appUrl."
    exit 0
}

if (Test-SentinEXHealth) {
    Show-LaunchMessage "Port 8501 is serving an app that this launcher did not start. Close that app first, then launch SentinEX again."
    exit 1
}

$portInUse = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($portInUse) {
    Show-LaunchMessage 'Port 8501 is already in use. Close the other app using it, then launch SentinEX again.'
    exit 1
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdoutLog = Join-Path $logDirectory "sentinex-$timestamp.stdout.log"
$stderrLog = Join-Path $logDirectory "sentinex-$timestamp.stderr.log"
foreach ($pattern in @('sentinex-*.stdout.log', 'sentinex-*.stderr.log')) {
    Get-ChildItem -LiteralPath $logDirectory -Filter $pattern -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 4 |
        Remove-Item -Force
}

try {
    $serverProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @(
            '-m', 'streamlit', 'run', 'app.py',
            '--server.address', '127.0.0.1',
            '--server.port', '8501',
            '--server.headless', 'true'
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    @{ ProcessId = $serverProcess.Id; StartedAt = (Get-Date).ToString('o') } |
        ConvertTo-Json |
        Set-Content -LiteralPath $pidFile -Encoding UTF8

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($serverProcess.HasExited) { throw 'The Streamlit process exited during startup.' }
        if (Test-SentinEXHealth) {
            Start-Process $appUrl
            exit 0
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'SentinEX did not respond within 45 seconds.'
} catch {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Show-LaunchMessage "SentinEX could not start:`n$($_.Exception.Message)`n`nLogs are saved in:`n$logDirectory"
    exit 1
}
