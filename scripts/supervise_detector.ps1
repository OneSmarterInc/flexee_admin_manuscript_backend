$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$mutex = [System.Threading.Mutex]::new($false, 'Local\FlexeeDetectorSupervisor')
$owns = $false
try {
    try { $owns = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owns = $true }
    if (-not $owns) { exit 0 }
    $env:Q_CLUSTER_NAME = 'flexee_detector'
    $pidFile = Join-Path $logs 'detector-supervised-process.json'
    while ($true) {
        # Reattach after a supervisor restart instead of starting a duplicate worker.
        $worker = $null
        if (Test-Path -LiteralPath $pidFile) {
            try {
                $saved = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
                $candidate = Get-Process -Id $saved.pid -ErrorAction Stop
                if ($candidate.StartTime.ToUniversalTime().Ticks.ToString() -eq $saved.started -and $candidate.Path -eq $python) { $worker = $candidate }
            } catch { }
        }
        if (-not $worker) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $worker = Start-Process -FilePath $python -ArgumentList 'manage.py','qcluster' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "detector-$stamp.out.log") -RedirectStandardError (Join-Path $logs "detector-$stamp.err.log") -PassThru
            @{pid=$worker.Id;started=$worker.StartTime.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json | Set-Content -LiteralPath $pidFile
            Add-Content -LiteralPath (Join-Path $logs 'detector-supervisor.log') -Value "$(Get-Date -Format o) Started detector worker $($worker.Id)"
        }
        $worker.WaitForExit()
        Add-Content -LiteralPath (Join-Path $logs 'detector-supervisor.log') -Value "$(Get-Date -Format o) Worker exited; restarting in 15 seconds."
        Start-Sleep -Seconds 15
    }
} finally {
    if ($owns) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
