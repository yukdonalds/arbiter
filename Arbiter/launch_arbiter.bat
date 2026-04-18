@echo off
rem =============================================================================
rem Arbiter daily launcher — use with Windows Task Scheduler
rem -----------------------------------------------------------------------------
rem Task Scheduler (Create Task):
rem   Action:  Start a program
rem   Program:  Full path to THIS file (launch_arbiter.bat)
rem   Start in: Folder containing this file (same as Arbiter project root)
rem   Trigger: Daily at your chosen time (before/alongside RTH as you prefer)
rem
rem Prereqs: IB Gateway or TWS running with API enabled (see config IB_HOST/IB_PORT).
rem          Python on PATH, or edit the line below to use a full python.exe path.
rem =============================================================================

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found in PATH. Install Python or set PATH, or edit this script to call python.exe by full path.
    exit /b 1
)

python "Arbiter Launch.py"
exit /b %ERRORLEVEL%
