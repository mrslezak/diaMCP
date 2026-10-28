@echo off
REM Start diaMCP natively on Windows from cmd.exe (e.g. the Miniforge Prompt).
REM Usage: start.cmd [host] [port]

setlocal
cd /d "%~dp0"

if "%~1"=="" (set "MCP_HOST=127.0.0.1") else (set "MCP_HOST=%~1")
if "%~2"=="" (set "MCP_PORT=8000") else (set "MCP_PORT=%~2")
set "PYTHONPATH=%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Open the Miniforge Prompt, or install Python 3.10+ from https://python.org
    exit /b 1
)

if not exist "workspace\tools" mkdir "workspace\tools"

echo Starting diaMCP on http://%MCP_HOST%:%MCP_PORT%/mcp
echo Press Ctrl+C to stop.
python server.py
exit /b %ERRORLEVEL%
