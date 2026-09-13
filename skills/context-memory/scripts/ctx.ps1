<#
.SYNOPSIS
Run the Context Manager from any repository.

.DESCRIPTION
The CLIs live in the memory-optimization checkout and need its root on
sys.path, so running them from another project fails. This launcher removes
that coupling: it holds the absolute paths, reads connection settings out of
server/.env, and works from whatever directory you happen to be in.

Scope handling, which is the part that matters:

  READ   always this repository plus the shared `global` scope. Both, every
         time - a lesson worth remembering everywhere is useless if it only
         surfaces in the repo where it was learned.

  WRITE  never guessed. `store` requires -Scope repo|shared explicitly,
         because a repository-specific fact written to the shared scope
         surfaces on unrelated work as if it were universal truth, and nothing
         about the write looks wrong at the time.

The repository scope key is derived from the git remote, falling back to the
directory name, so the same project keeps one key across clones.

.EXAMPLE
ctx.ps1 build -Task "why does the pgvector column width matter"
ctx.ps1 search -Query "embedding provider"
ctx.ps1 store -Scope repo -Kind discovery -Topic server.ports -Text "..."
ctx.ps1 health
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('build', 'search', 'store', 'extract', 'promote', 'eval', 'health', 'flush')]
    [string]$Command = 'health',

    [string]$Task,
    [string]$Query,
    [string]$Text,
    [string]$Topic,
    [string[]]$Files = @(),

    [ValidateSet('repo', 'shared')]
    [string]$Scope,

    [ValidateSet('decision', 'discovery', 'lesson', 'convention', 'failure', 'incident', 'note')]
    [string]$Kind = 'note',

    [string]$Id,
    [ValidateSet('durable', 'stale', 'superseded')]
    [string]$To,
    [string]$Replacement,

    [string]$Since = 'HEAD~10',
    [int]$MaxTokens = 8000,
    [int]$TopK = 10,
    # Similarity search always returns nearest neighbours: a query matching
    # nothing still comes back with rows at plausible-looking scores (measured:
    # a nonsense query scored 0.50-0.55 against real memories at 0.65-0.78).
    # No default is set here - picking a cutoff from a couple of observations is
    # guessing. Read the printed score, and pass -Threshold when you want a floor.
    [double]$Threshold = -1,
    [double]$Confidence = 0.5,
    [double]$Importance = 0.5,
    [string[]]$Tag = @(),

    [switch]$ReportOnly,
    [switch]$Store,
    [switch]$Json,
    [switch]$NoMemory,
    [switch]$List
)

$ErrorActionPreference = 'Stop'

# Captured before any Push-Location: the caller's directory decides the scope
# key and is what `extract` reads git history from.
$CallerDir = (Get-Location).Path

# Probing git for the repo name legitimately fails outside a repository, and
# that failure must not become this script's exit code - a caller checking it
# would read a successful health check as a failure.
$script:ExitCode = 0

# Where the Context Manager lives, in order of preference:
#   1. CONTEXT_MEMORY_HOME, for a checkout that has moved
#   2. home.txt beside this script, written at install time
#   3. three levels up, for running straight out of the checkout
# No hardcoded path literal: this file is read as ANSI by PowerShell 5.1 unless
# it carries a UTF-8 BOM, and a non-ASCII repo path then silently decodes wrong
# (measured: "Gestión" became "GestiÃ³n" and the launcher reported the venv
# missing). home.txt is read with an explicit encoding, which has no such trap.
$HomeFile = Join-Path $PSScriptRoot 'home.txt'
$Home_ =
    if ($env:CONTEXT_MEMORY_HOME) { $env:CONTEXT_MEMORY_HOME }
    elseif (Test-Path $HomeFile) { (Get-Content $HomeFile -Encoding UTF8 -TotalCount 1).Trim() }
    else { (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path }
$Python = Join-Path $Home_ '.venv\Scripts\python.exe'
$EnvFile = Join-Path $Home_ 'server\.env'

if (-not (Test-Path $Python)) {
    Write-Error "Context Manager python not found at $Python. Set CONTEXT_MEMORY_HOME to the memory-optimization checkout."
}

function Get-EnvValue {
    param([string]$Name, [string]$Default = '')
    if (Test-Path $EnvFile) {
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
                if ($Matches[1]) { return $Matches[1] }
            }
        }
    }
    return $Default
}

function Get-RepoKey {
    <#
      Derive a stable scope key for the current repository. The git remote is
      preferred over the folder name: two clones of one project in differently
      named directories must not end up with two separate memory scopes.
    #>
    try {
        $remote = & git config --get remote.origin.url 2>$null
        if ($LASTEXITCODE -eq 0 -and $remote) {
            $name = ($remote -replace '\.git$', '') -split '[/:]' | Select-Object -Last 1
            if ($name) { return $name }
        }
    } catch { }
    try {
        $top = & git rev-parse --show-toplevel 2>$null
        if ($LASTEXITCODE -eq 0 -and $top) { return (Split-Path $top -Leaf) }
    } catch { }
    return (Split-Path (Get-Location) -Leaf)
}

$ApiPort = Get-EnvValue -Name 'MEM0_API_PORT' -Default '8888'
$env:MEM0_API_URL = "http://localhost:$ApiPort"
$env:MEM0_API_KEY = Get-EnvValue -Name 'ADMIN_API_KEY'
# From server/.env, not from $env:USERNAME. MEM0_USER is part of the search
# filter: pick it up from the machine account and every memory written under a
# different identity becomes invisible, with the search returning nothing at all
# rather than an error. Measured: searching as "Witbor" for memories stored as
# "lautaro" returned 0 of 10 twice, with no indication why.
$env:MEM0_USER = Get-EnvValue -Name 'MEM0_USER' -Default $env:USERNAME

$RepoKey = Get-RepoKey
$env:MEM0_REPOSITORY = $RepoKey

# The CLIs are invoked as modules from the checkout, so run from there and let
# the caller's directory only decide the scope key.
Push-Location $Home_
try {
    switch ($Command) {
        'health' {
            $api = try { (Invoke-WebRequest -Uri "$($env:MEM0_API_URL)/docs" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { 'down' }
            Write-Host "Context Manager: $Home_"
            Write-Host "API:             $api  ($($env:MEM0_API_URL))"
            Write-Host "repository scope: $RepoKey"
            Write-Host "shared scope:     global"
            Write-Host "user:             $($env:MEM0_USER)"

            # Queued writes are invisible until something says so. A memory
            # sitting in the spool is not stored, and nothing else will mention it.
            $pending = & $Python -c "import sys; sys.path.insert(0,'.'); from src.memory.spool import Spool; s=Spool(); print(s.pending()); print(s.root)"
            $count = [int]($pending | Select-Object -First 1)
            $spoolRoot = ($pending | Select-Object -Last 1)
            Write-Host "spool:            $count queued  ($spoolRoot)"
            if ($count -gt 0) {
                Write-Host ""
                Write-Host "$count memory write(s) are queued and NOT stored. Replay them with:" -ForegroundColor Yellow
                Write-Host "  & '$PSCommandPath' flush" -ForegroundColor Yellow
            }
            if ($api -eq 'down') {
                Write-Host ""
                Write-Host "Start it with: & '$Home_\scripts\stack.ps1' up" -ForegroundColor Yellow
                $script:ExitCode = 1
            } else {
                $script:ExitCode = 0
            }
        }
        'build' {
            if (-not $Task) { Write-Error "build needs -Task '<what you are about to do>'" }
            $a = @('-m', 'src.scripts.context_build', '--task', $Task, '--repository', $RepoKey, '--max-tokens', $MaxTokens, '--top-k', $TopK)
            if ($Files.Count) { $a += '--files'; $a += $Files }
            if ($ReportOnly) { $a += '--report-only' }
            if ($Json) { $a += '--json' }
            if ($NoMemory) { $a += '--no-memory' }
            & $Python @a
        }
        'search' {
            if (-not $Query) { Write-Error "search needs -Query '<what you are looking for>'" }
            # Read is always repo + shared. Two calls rather than one, so each
            # result set is labelled and a hit cannot be mistaken for the other scope.
            $common = @('-m', 'src.scripts.memory_search', '--query', $Query, '--top-k', $TopK)
            if ($Threshold -ge 0) { $common += @('--threshold', $Threshold) }
            if ($Json) { $common += '--json' }
            Write-Host "--- repository: $RepoKey ---"
            $repoOut = & $Python @common --scope repository --key $RepoKey
            $repoOut | Out-Host
            Write-Host "--- shared: global ---"
            $sharedOut = & $Python @common --scope global --key global
            $sharedOut | Out-Host

            # Nothing anywhere is ambiguous: an empty store and a wrong identity
            # or scope key produce the identical output. Say which it could be
            # rather than letting the reader assume the store is empty.
            # Count from the CLI's own "N of top_k=..." line rather than pattern
            # matching result rows. Matching rows misfired and printed "no
            # matches" while a result was on screen - a false alarm trains the
            # reader to ignore the warning, which is the one outcome worth
            # avoiding for a message about a silent failure.
            $total = 0
            foreach ($line in @($repoOut) + @($sharedOut)) {
                if ("$line" -match '^\s*(\d+) of top_k') { $total += [int]$Matches[1] }
            }
            $found = $total -gt 0
            if (-not $found) {
                Write-Host ""
                Write-Host "No matches in either scope. That is indistinguishable from a wrong lookup, so check:" -ForegroundColor Yellow
                Write-Host "  identity  MEM0_USER=$($env:MEM0_USER)   (from server/.env; memories written under another value are invisible)" -ForegroundColor Yellow
                Write-Host "  repo key  $RepoKey   (from the git remote)" -ForegroundColor Yellow
                Write-Host "  contents  & '$PSCommandPath' health" -ForegroundColor Yellow
            }
        }
        'store' {
            if (-not $Scope) {
                Write-Error "store needs -Scope repo|shared. This is deliberately not guessed: a repository fact written to the shared scope surfaces on unrelated work as universal truth, and nothing about the write looks wrong at the time."
            }
            if (-not $Text) { Write-Error "store needs -Text '<the memory>'" }
            $scopeName = if ($Scope -eq 'repo') { 'repository' } else { 'global' }
            $scopeKey = if ($Scope -eq 'repo') { $RepoKey } else { 'global' }
            $a = @('-m', 'src.scripts.memory_store', '--scope', $scopeName, '--key', $scopeKey,
                   '--kind', $Kind, '--text', $Text, '--confidence', $Confidence, '--importance', $Importance)
            if ($Topic) { $a += @('--topic', $Topic) }
            foreach ($t in $Tag) { $a += @('--tag', $t) }
            if ($Json) { $a += '--json' }
            & $Python @a
        }
        'extract' {
            $a = @('-m', 'src.scripts.memory_extract', '--repository', $RepoKey, '--since', $Since, '--root', $CallerDir)
            if ($Store) { $a += '--store' }
            if ($Json) { $a += '--json' }
            & $Python @a
        }
        'promote' {
            if (-not $Id -or -not $To) { Write-Error "promote needs -Id <memory id> -To durable|stale|superseded" }
            $a = @('-m', 'src.scripts.memory_promote', '--id', $Id, '--to', $To)
            if ($Replacement) { $a += @('--replacement', $Replacement) }
            if ($Json) { $a += '--json' }
            & $Python @a
        }
        'eval' {
            & $Python -m src.scripts.context_eval --seed
        }
        'flush' {
            $a = @('-m', 'src.scripts.memory_flush')
            if ($List) { $a += '--list' }
            if ($Json) { $a += '--json' }
            & $Python @a
        }
    }
    if ($Command -ne 'health') { $script:ExitCode = $LASTEXITCODE }
} finally {
    Pop-Location
}

exit $script:ExitCode
