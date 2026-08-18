@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  start "" pyw -3.11 "%~dp0codex_agent_console.py"
  exit /b 0
)
start "" pythonw "%~dp0codex_agent_console.py"
