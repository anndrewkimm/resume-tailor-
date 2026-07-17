$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

if (-not (Test-Path "backend/.env")) {
    Write-Error "backend/.env is missing. Copy backend/.env.example, then set SHARED_SECRET and ANTHROPIC_API_KEY."
}

python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765
