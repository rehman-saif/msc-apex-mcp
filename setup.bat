@echo off
rem Sets up the Apex MCP server. Safe to re-run: it also updates the skills.
rem   1. .venv with the MCP SDK (your Python 3.10+; Apex's own Python is not touched)
rem   2. config.json with a fresh bridge_token (kept if it already exists)
rem   3. the nastran-fea-skills repo in vendor\ (the server checks Apex Python against it)
rem   4. MCP client config snippets with this folder's paths
rem   5. optionally: Claude Code plugin + MCP server registration
rem Run "setup.bat /y" to configure Claude Code without asking.
setlocal
cd /d "%~dp0"

set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not exist "%PYEXE%" (
  for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
)
if not exist "%PYEXE%" (
  echo Could not find Python 3.10+ . Edit PYEXE in setup.bat.
  exit /b 1
)
echo Using base Python: %PYEXE%

if not exist ".venv\Scripts\python.exe" (
  "%PYEXE%" -m venv .venv || exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || exit /b 1
".venv\Scripts\python.exe" -c "import mcp, sys; print('OK: mcp installed in', sys.prefix)"
if not exist output mkdir output

set "CC="
if /i "%~1"=="/y" (
  set "CC=--claude-code"
) else (
  where claude >nul 2>&1 && (
    choice /c YN /m "Configure Claude Code (install the FEA skills plugin and register this MCP server)"
    if not errorlevel 2 set "CC=--claude-code"
  )
)
".venv\Scripts\python.exe" apex_mcp\setup_helper.py %CC% || exit /b 1

echo.
echo Done. Next:
echo   1. In Apex: Options - Application Settings - Custom Tools - add %CD%\apex_palette
echo   2. Open a model, then Claude palette - Claude Bridge - Start
echo   3. Other MCP clients: copy mcp_config.snippet.json or codex_config.snippet.toml
endlocal
