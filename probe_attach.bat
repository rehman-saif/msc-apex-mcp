@echo off
rem Usage: probe_attach.bat <apex_pid>   (Apex open; read-only)
setlocal
cd /d "%~dp0"
"C:\Program Files\MSC.Software\MSC Apex\2024-011290\python3\python.exe" -u probe_attach.py %*
echo.
pause
endlocal
