@echo off
REM Dublin Padel Monitor — Scheduled Snapshot Pass Runner
REM Invoked automatically by Windows Task Scheduler at 08:00, 12:30, 16:30, 20:00, and 23:30.

cd /d "%~dp0\.."
echo [%DATE% %TIME%] Running scheduled snapshot wave... >> logs\scheduled_runs.log
python monitor.py snapshot-wave >> logs\scheduled_runs.log 2>&1
echo [%DATE% %TIME%] Scheduled snapshot wave completed. >> logs\scheduled_runs.log

