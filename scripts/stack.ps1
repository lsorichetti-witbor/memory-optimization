<#
.SYNOPSIS
Windows-friendly control for the self-hosted Mem0 stack.

.DESCRIPTION
server/Makefile's `up` target shells out to `lsof`, which does not exist on
Windows, so the Makefile is unusable here. This wraps `docker compose` directly.

Only the `mem0` and `postgres` services are started. The Next.js dashboard is
optional and is not needed by the Context Manager.

.EXAMPLE
./scripts/stack.ps1 up
./scripts/stack.ps1 health
./scripts/stack.ps1 logs
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'restart', 'logs', 'health', 'reset')]
    [string]$Command = 'health',

    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$ServerDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'server'
$ApiUrl = 'http://localhost:8888'
$Services = @('postgres', 'mem0')

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    Push-Location $ServerDir
    try { & docker compose @ComposeArgs; return $LASTEXITCODE }
    finally { Pop-Location }
}

function Test-Port {
    param([int]$Port)
    $listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return $null -ne $listening
}

function Wait-Api {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "$ApiUrl/auth/setup-status" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 3 }
    }
    return $false
}

switch ($Command) {
    'up' {
        foreach ($port in 8888, 8432) {
            if (Test-Port -Port $port) {
                Write-Error "port $port is already in use. Find the owner with: Get-NetTCPConnection -State Listen -LocalPort $port"
            }
        }
        $rc = Invoke-Compose @('up', '-d', '--build') + $Services
        if ($rc -ne 0) { Write-Error "docker compose up failed with exit code $rc" }
        if (Wait-Api) {
            Write-Host "Stack is ready. API: $ApiUrl  (OpenAPI at $ApiUrl/docs)"
        } else {
            # Never report success on a timeout: an unreachable API that prints
            # "ready" is worse than one that prints nothing.
            Invoke-Compose @('logs', '--tail', '40', 'mem0') | Out-Host
            Write-Error "API did not answer within $TimeoutSeconds seconds."
        }
    }
    'down' { Invoke-Compose @('down') | Out-Null }
    'restart' {
        Invoke-Compose @('restart') + $Services | Out-Null
        if (-not (Wait-Api)) { Write-Error "API did not come back within $TimeoutSeconds seconds." }
    }
    'logs' { Invoke-Compose @('logs', '-f') + $Services }
    'reset' {
        Write-Host 'This deletes the postgres volume and every stored memory.' -ForegroundColor Yellow
        $answer = Read-Host 'Type RESET to confirm'
        if ($answer -ne 'RESET') { Write-Host 'aborted'; return }
        Invoke-Compose @('down', '-v') | Out-Null
    }
    'health' {
        $api = try { (Invoke-WebRequest -Uri "$ApiUrl/docs" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 'down' }
        Write-Host "API:      $api"
        Push-Location $ServerDir
        try {
            & docker compose exec -T postgres pg_isready -q
            Write-Host "Postgres: $(if ($LASTEXITCODE -eq 0) { 'ok' } else { 'down' })"
            & docker compose ps --format "{{.Service}}`t{{.Status}}"
        } finally { Pop-Location }
    }
}
