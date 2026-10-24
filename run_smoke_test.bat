@echo off
rem Run with Apex OPEN. Optional arguments: an Apex PID and/or a .h5 results file.
rem Example:  run_smoke_test.bat "C:\path\to\run.h5"
setlocal
cd /d "%~dp0"
set "APEXPY=C:\Program Files\MSC.Software\MSC Apex\2024-011290\python3\python.exe"
"%APEXPY%" -u smoke_test.py %*
echo.
pause
endlocal
