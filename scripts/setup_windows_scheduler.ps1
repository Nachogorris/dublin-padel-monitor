# Registers 5 automated snapshot triggers in Windows Task Scheduler
# Times: 08:00 (Morning), 12:30 (Lunch), 16:30 (Pre-Peak), 20:00 (Prime Peak), 23:30 (Night Lock-in)

$ProjectDir = (Get-Item $PSScriptRoot).Parent.FullName
$BatPath = Join-Path $PSScriptRoot "run_scheduled_pass.bat"
$TaskName = "DublinPadelMonitor_AutoSnapshots"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "Registering Windows Task Scheduler: $TaskName" -ForegroundColor Cyan
Write-Host "Project Directory: $ProjectDir" -ForegroundColor Gray
Write-Host "Batch Executable : $BatPath" -ForegroundColor Gray
Write-Host "========================================================" -ForegroundColor Cyan

# Ensure logs directory exists
$LogsDir = Join-Path $ProjectDir "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ProjectDir

# Create 5 daily triggers
$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "08:00"),
    (New-ScheduledTaskTrigger -Daily -At "12:30"),
    (New-ScheduledTaskTrigger -Daily -At "16:30"),
    (New-ScheduledTaskTrigger -Daily -At "20:00"),
    (New-ScheduledTaskTrigger -Daily -At "23:30")
)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings -Description "Dublin Padel Monitor Automated Court Snapshot Telemetry & Occupancy Wave" -Force

Write-Host "`nTask '$TaskName' successfully registered in Windows Task Scheduler!" -ForegroundColor Green
Write-Host "Triggers: 08:00, 12:30, 16:30, 20:00, 23:30 daily." -ForegroundColor Green
Write-Host "Snapshots will now run automatically in the background without manual effort." -ForegroundColor Green

