param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw 'Create .venv and install requirements.txt first. See README.md.'
    }
    $pgCtlPath = Join-Path $projectRoot '.runtime\pgsql\bin\pg_ctl.exe'
    $pgDataPath = Join-Path $projectRoot '.runtime\pgdata'
    if ((Test-Path -LiteralPath $pgCtlPath) -and (Test-Path -LiteralPath $pgDataPath)) {
        & $pgCtlPath -D $pgDataPath status
        if ($LASTEXITCODE -ne 0) {
            & $pgCtlPath -D $pgDataPath -l (Join-Path $projectRoot '.runtime\postgres.log') -o '-p 5432 -h 127.0.0.1' start
            if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL failed to start.' }
        }
    } elseif (Get-Command docker -ErrorAction SilentlyContinue) {
        docker compose up -d
        if ($LASTEXITCODE -ne 0) { throw 'Docker databases failed to start.' }
    } else {
        throw 'Start PostgreSQL separately and configure .env. See README.md.'
    }
    & $pythonPath -m uvicorn app.main:app --reload --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}
