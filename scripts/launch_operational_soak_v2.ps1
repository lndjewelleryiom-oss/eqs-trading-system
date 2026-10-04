param([string]$Root = "C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
$ErrorActionPreference = "Stop"
$python = (Get-Command python).Source
$script = Join-Path $Root "scripts\operational_commissioning_soak_v2.py"
$logDir = Join-Path $Root "artifacts\commissioning"
$stdout = Join-Path $logDir "soak_v2_detached_stdout.log"
$stderr = Join-Path $logDir "soak_v2_detached_stderr.log"
$task = "EQS_Operational_Soak_V2"
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $script + '"') -WorkingDirectory $Root
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 26) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
$info=Get-ScheduledTaskInfo -TaskName $task
[pscustomobject]@{TaskName=$task;State=(Get-ScheduledTask -TaskName $task).State;LastRunTime=$info.LastRunTime;LastTaskResult=$info.LastTaskResult}|ConvertTo-Json -Compress
