@echo off
setlocal
cd /d "%~dp0"

echo Starting Mission Control server on http://127.0.0.1:8420 ...
start "" http://127.0.0.1:8420
uv run mission-control-server

endlocal
