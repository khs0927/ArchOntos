<#
.SYNOPSIS
  Throwaway pgvector PostgreSQL for the integration tests, on the light "docker-lite" WSL
  engine (Alpine dockerd) instead of Docker Desktop.

.EXAMPLE
  ./ops/test-db-lite.ps1 up        # starts the container, prints the URL to export
  ./ops/test-db-lite.ps1 test      # up + full pytest + down
  ./ops/test-db-lite.ps1 down
#>
param(
    [ValidateSet('up', 'down', 'test')][string]$Action = 'test',
    [string]$Distro = 'docker-lite',
    [int]$Port = 55480,
    [string]$Name = 'archontos-test-pg',
    [string]$Image = 'pgvector/pgvector:pg18'
)
$ErrorActionPreference = 'Stop'
$url = "postgresql://postgres:test@127.0.0.1:$Port/postgres"

function Invoke-LiteDocker([string[]]$DockerArgs) {
    & wsl.exe -d $Distro -- docker @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw "docker $($DockerArgs -join ' ') failed ($LASTEXITCODE)" }
}

function Start-Db {
    $free = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
    if ($free -lt 1.5) { throw ("Only {0:N1} GB RAM free; refusing to start PostgreSQL." -f $free) }
    & wsl.exe -d $Distro -- docker rm -f $Name 2>$null | Out-Null
    Invoke-LiteDocker @('run', '-d', '--rm', '--name', $Name, '-e', 'POSTGRES_PASSWORD=test',
        '-p', "${Port}:5432", $Image) | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        & wsl.exe -d $Distro -- docker exec $Name pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep 1
    }
    if (-not (Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet)) {
        throw "PostgreSQL is not reachable on 127.0.0.1:$Port (WSL localhost forwarding off?)"
    }
    Write-Host "ARCHONTOS_TEST_DATABASE_URL=$url"
}

function Stop-Db { & wsl.exe -d $Distro -- docker rm -f $Name 2>$null | Out-Null }

switch ($Action) {
    'up' { Start-Db }
    'down' { Stop-Db }
    'test' {
        Start-Db
        try {
            $env:ARCHONTOS_TEST_DATABASE_URL = $url
            python -m pytest -q
            $code = $LASTEXITCODE
        } finally { Stop-Db }
        exit $code
    }
}
