<#
.SYNOPSIS
Windows-friendly control for the self-hosted Mem0 stack.

.DESCRIPTION
server/Makefile's `up` target shells out to `lsof`, which does not exist on
Windows, so the Makefile is unusable here. This wraps `docker compose` directly.

Starts postgres, the API and the Next.js dashboard. Host ports come from
server/.env (MEM0_API_PORT, POSTGRES_HOST_PORT, DASHBOARD_PORT) so this script
and docker-compose.yaml cannot disagree about where the stack is listening.

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
$Services = @('postgres', 'mem0', 'mem0-dashboard')

function Get-EnvValue {
    <#
      Read a value from server/.env. Ports live there, not here: hardcoding them
      in this script as well as in docker-compose.yaml is two sources of truth
      that drift, and the drift shows up as "the stack is down" when it is
      merely listening somewhere else.
    #>
    param([string]$Name, [string]$Default)
    $envFile = Join-Path $ServerDir '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
                $value = $Matches[1]
                if ($value) { return $value }
            }
        }
    }
    return $Default
}

$ApiPort = Get-EnvValue -Name 'MEM0_API_PORT' -Default '8888'
$PgPort = Get-EnvValue -Name 'POSTGRES_HOST_PORT' -Default '8432'
$DashPort = Get-EnvValue -Name 'DASHBOARD_PORT' -Default '3000'
$ApiUrl = "http://localhost:$ApiPort"
$DashboardUrl = "http://localhost:$DashPort"

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

function Test-Api {
    try {
        $r = Invoke-WebRequest -Uri "$ApiUrl/auth/setup-status" -UseBasicParsing -TimeoutSec 3
        return $r.StatusCode -eq 200
    } catch { return $false }
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
        # A port held by OUR already-running stack is not a conflict. Erroring on
        # it sends the reader hunting a process when nothing is wrong, and
        # `docker compose up -d` is idempotent anyway. Only a port held by
        # something that is not this stack is worth stopping for.
        $oursAlready = Test-Api
        if (-not $oursAlready) {
            foreach ($port in $ApiPort, $PgPort) {
                if (Test-Port -Port $port) {
                    Write-Error "port $port is in use, and it is not this stack's API. Find the owner with: Get-NetTCPConnection -State Listen -LocalPort $port"
                }
            }
        }
        $rc = Invoke-Compose @('up', '-d', '--build') + $Services
        if ($rc -ne 0) { Write-Error "docker compose up failed with exit code $rc" }
        if (Wait-Api) {
            Write-Host "Stack is ready. API: $ApiUrl  (OpenAPI at $ApiUrl/docs)  Dashboard: $DashboardUrl"
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
        $dash = try { (Invoke-WebRequest -Uri "$DashboardUrl/api/health" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 'down' }
        Write-Host "API:       $api  ($ApiUrl)"
        Write-Host "Dashboard: $dash  ($DashboardUrl)"
        Push-Location $ServerDir
        try {
            & docker compose exec -T postgres pg_isready -q
            Write-Host "Postgres: $(if ($LASTEXITCODE -eq 0) { 'ok' } else { 'down' })"
            & docker compose ps --format "{{.Service}}`t{{.Status}}"
        } finally { Pop-Location }
    }
}
