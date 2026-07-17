@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\.env" (
  echo backend\.env is missing. Run configure.cmd first.
  exit /b 1
)
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765
