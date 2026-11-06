$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'supervise_detector.ps1'
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Flexee AI Detector Worker' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Runs the local Flexee detector queue at sign-in and restarts it on exit.' -Force | Out-Null
Start-ScheduledTask -TaskName 'Flexee AI Detector Worker'
Write-Output 'Detector automatic startup installed and started.'
