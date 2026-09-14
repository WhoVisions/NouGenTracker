# Register NouGen-TrackerDaily: runs tracker_daily_publish.ps1 hidden every 4h,
# catching up after sleep/boot (StartWhenAvailable). Idempotent: replaces the
# task if it exists. Then runs it once and prints the log tail.
$Bin = Join-Path $env:USERPROFILE '.nougen\bin'
$Script = Join-Path $Bin 'tracker_daily_publish.ps1'
$Vbs = Join-Path $Bin 'run_hidden.vbs'
if (-not (Test-Path $Script)) { throw "missing $Script" }
if (-not (Test-Path $Vbs)) { throw "missing $Vbs" }

$Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" `
  -Argument ('//B //Nologo "{0}" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f $Vbs, $Script)
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 4)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'NouGen-TrackerDaily' -Action $Action -Trigger $Trigger -Settings $Settings `
  -Principal $Principal -Description 'Export + publish NouGenTracker dailies (yesterday..today) every 4h' -Force | Out-Null

$Log = Join-Path $env:USERPROFILE '.nougen\logs\tracker_daily.log'
$before = if (Test-Path $Log) { (Get-Content $Log).Count } else { 0 }
Start-ScheduledTask -TaskName 'NouGen-TrackerDaily'
# wscript returns immediately, so wait on the log, not the task state.
$deadline = (Get-Date).AddMinutes(9)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 10
  if ((Test-Path $Log) -and (Get-Content $Log).Count -gt $before) {
    $last = (Get-Content $Log)[-1]
    if ($last -match 'published|no change|SKIP|FAIL|FATAL') { break }
  }
}
$i = Get-ScheduledTaskInfo -TaskName 'NouGen-TrackerDaily'
"task: next run {0}" -f $i.NextRunTime
"log:"; if (Test-Path $Log) { Get-Content $Log -Tail 4 }
