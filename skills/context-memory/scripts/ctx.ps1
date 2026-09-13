<#
.SYNOPSIS
Run the Context Manager from any repository.

.DESCRIPTION
The CLIs live in the memory-optimization checkout and need its root on
sys.path, so running them from another project fails. This launcher removes
that coupling: it holds the absolute paths, reads connection settings out of
server/.env, and works from whatever directory you happen to be in.

Scope handling, which is the part that matters:

  READ   always this repository plus the `global` scope. Both, every
         time - a lesson worth remembering everywhere is useless if it only
         surfaces in the repo where it was learned.

  WRITE  this repository by default. -Scope global is opt-in, because the two
         mistakes are not symmetric: a general lesson stuck in one repo is
         merely missed elsewhere, while a repo-specific fact written to the
         global scope surfaces on unrelated work as universal truth and nothing
         about the write looks wrong at the time.

The repository scope key is derived from the git remote, falling back to the
directory name, so the same project keeps one key across clones.

.EXAMPLE
ctx.ps1 build -Task "why does the pgvector column width matter"
ctx.ps1 search -Query "embedding provider"
ctx.ps1 store -Kind discovery -Topic server.ports -Text "..."      # this repo
ctx.ps1 store -Scope global -Kind lesson -Text "..."               # everywhere
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

    # Defaults to this repository. `global` has to be asked for, because the two
    # mistakes are not symmetric: a general lesson stuck in one repo is merely
    # missed elsewhere, while a repo-specific fact written to `global` surfaces
    # on unrelated projects as universal truth.
    [ValidateSet('repo', 'global')]
    [string]$Scope = 'repo',

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

function New-TextFile {
    <#
      Hand free text to python through a file, never through argv.

      PowerShell 5.1 re-splits a quoted argument on its way to a native
      executable and strips the quotes: measured, `a memory containing "quoted
      text" in the middle` arrives as THREE arguments and argv[1] is `a memory
      containing quoted`. So any memory or query containing a double quote was
      truncated or rejected - and memories about an error are exactly the ones
      full of quotes.

      A file has no quoting rules at all.
    #>
    param([string]$Text)
    $f = Join-Path $env:TEMP ("ctx-text-{0}.txt" -f [guid]::NewGuid().ToString('N').Substring(0, 12))
    [System.IO.File]::WriteAllText($f, $Text, (New-Object System.Text.UTF8Encoding($false)))
    return $f
}

function Show-Backlog {
    <#
      Say how many writes are queued, after any command that writes.

      A queued write is not a stored write, and nothing else in the output
      mentions it. Without this line the queue can grow for weeks while every
      individual command looks like it worked - a slow leak that presents as
      "memory just doesn't seem to remember much".

      Deliberately silent when the queue is empty: a line that prints on every
      successful command is a line people stop reading.
    #>
    try {
        $info = & $Python -c "import sys; sys.path.insert(0,'.'); from src.memory.spool import Spool; s=Spool(); print(s.pending()); print(s.dead())"
        $queued = [int]($info | Select-Object -First 1)
        $dead = [int]($info | Select-Object -Last 1)
    } catch { return }
    if ($queued -gt 0) {
        Write-Host "spool: $queued write(s) queued and not stored - & '$PSCommandPath' flush" -ForegroundColor Yellow
    }
    if ($dead -gt 0) {
        Write-Host "spool: $dead write(s) in dead/ gave up after repeated failures" -ForegroundColor Red
    }
}

$ApiPort = Get-EnvValue -Name 'MEM0_API_PORT' -Default '8888'
$env:MEM0_API_URL = "http://localhost:$ApiPort"
$env:MEM0_API_KEY = Get-EnvValue -Name 'ADMIN_API_KEY'
# Identity precedence: an explicitly set MEM0_USER, then server/.env, then the
# machine account.
#
# The machine account is last for a measured reason: MEM0_USER is part of the
# search filter, so picking it up from $env:USERNAME writes memories under an
# identity nothing later queries, and the search returns nothing at all rather
# than an error. Searching as "Witbor" for memories stored as "lautaro" returned
# 0 of 10 twice, with no indication why.
#
# But .env used to win over an explicit value too, and that is a different
# thing: it meant a second person on one machine could not write as themselves -
# their memory silently landed under whoever .env named. An explicit MEM0_USER
# is a deliberate statement of identity and outranks the file.
$env:MEM0_USER = if ($env:MEM0_USER) { $env:MEM0_USER } else { Get-EnvValue -Name 'MEM0_USER' -Default $env:USERNAME }

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
            Write-Host "repo scope:      $RepoKey"
            Write-Host "global scope:    global"
            Write-Host "user:            $($env:MEM0_USER)"

            # Queued writes are invisible until something says so. A memory
            # sitting in the spool is not stored, and nothing else will mention it.
            $spoolInfo = & $Python -c "import sys; sys.path.insert(0,'.'); from src.memory.spool import Spool; s=Spool(); print(s.pending()); print(s.dead()); print(s.root)"
            $count = [int]($spoolInfo | Select-Object -First 1)
            $deadCount = [int]($spoolInfo | Select-Object -Skip 1 -First 1)
            $spoolRoot = ($spoolInfo | Select-Object -Last 1)
            Write-Host "spool:           $count queued, $deadCount dead  ($spoolRoot)"

            # The server has its own queue, for writes it accepted but could not
            # embed. Those are safe but NOT searchable, and the client spool says
            # nothing about them - a health check that showed only the local
            # queue would report all-clear while the store was incomplete.
            $srvPending = 0; $srvDead = 0; $srvBreaker = $null
            try {
                $resp = Invoke-RestMethod -Uri "$($env:MEM0_API_URL)/memories/pending" `
                    -Headers @{ 'X-API-Key' = $env:MEM0_API_KEY } -TimeoutSec 5
                $srvPending = [int]$resp.not_searchable
                $srvDead = [int]$resp.dead
                $srvBreaker = $resp.circuit_breaker
                Write-Host "server queue:    $srvPending not searchable ($($resp.pending) pending, $($resp.error) error, $srvDead dead)"
            } catch {
                Write-Host "server queue:    unavailable (older server, or the API is down)"
            }
            if ($srvPending -gt 0) {
                Write-Host ""
                Write-Host "$srvPending memory write(s) are stored on the server but NOT searchable yet." -ForegroundColor Yellow
                # No ?? operator in PowerShell 5.1 - it is a parser error, not a
                # fallback, and it takes the whole script down at load time.
                $dash = if ($env:DASHBOARD_URL) { $env:DASHBOARD_URL } else { 'http://localhost:3000' }
                Write-Host "  See them at $dash/dashboard/queue" -ForegroundColor Yellow
            }
            if ($srvBreaker -and $srvBreaker.open) {
                Write-Host ""
                Write-Host "Server embedding queue is PARKED: $($srvBreaker.code)" -ForegroundColor Red
                Write-Host "  $($srvBreaker.reason)" -ForegroundColor Red
                Write-Host "  next probe: $($srvBreaker.retry_at)" -ForegroundColor Red
            }
            if ($count -gt 0) {
                Write-Host ""
                Write-Host "$count memory write(s) are queued and NOT stored. Replay them with:" -ForegroundColor Yellow
                Write-Host "  & '$PSCommandPath' flush" -ForegroundColor Yellow
            }
            if ($deadCount -gt 0) {
                Write-Host ""
                Write-Host "$deadCount write(s) gave up after repeated failures and are in $spoolRoot\dead." -ForegroundColor Red
                Write-Host "They are still on disk. Read the last error, fix the cause, move them back." -ForegroundColor Red
            }
            if ($api -eq 'down') {
                Write-Host ""
                Write-Host "Start it with: & '$Home_\scripts\stack.ps1' up" -ForegroundColor Yellow
            }
            # Non-zero while anything is unstored OR unsearchable, not only when
            # the API is down. A green health check with writes sitting in either
            # queue is exactly the reassuring-but-wrong answer this whole layer
            # exists to avoid.
            if ($api -eq 'down' -or $count -gt 0 -or $deadCount -gt 0 -or $srvPending -gt 0) {
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
            # Read is always repo + global. Two calls rather than one, so each
            # result set is labelled and a hit cannot be mistaken for the other scope.
            $queryFile = New-TextFile -Text $Query
            $common = @('-m', 'src.scripts.memory_search', '--query-file', $queryFile, '--top-k', $TopK)
            if ($Threshold -ge 0) { $common += @('--threshold', $Threshold) }
            if ($Json) { $common += '--json' }
            Write-Host "--- repo: $RepoKey ---"
            $repoOut = & $Python @common --scope repo --key $RepoKey
            $repoOut | Out-Host
            Write-Host "--- global ---"
            $globalOut = & $Python @common --scope global --key global
            $globalOut | Out-Host

            # Nothing anywhere is ambiguous: an empty store and a wrong identity
            # or scope key produce the identical output. Say which it could be
            # rather than letting the reader assume the store is empty.
            # Count from the CLI's own "N of top_k=..." line rather than pattern
            # matching result rows. Matching rows misfired and printed "no
            # matches" while a result was on screen - a false alarm trains the
            # reader to ignore the warning, which is the one outcome worth
            # avoiding for a message about a silent failure.
            $total = 0
            foreach ($line in @($repoOut) + @($globalOut)) {
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
            if (-not $Text) { Write-Error "store needs -Text '<the memory>'" }
            # One word for one thing: `repo` is what you type, what the CLI
            # accepts, what lands in metadata["scope"], and what prefixes the
            # agent_id. It used to be translated to `repository` here, and when
            # the vocabulary dropped that alias the translation kept passing a
            # word argparse no longer accepted - store failed on every repo
            # write while the launcher itself looked untouched.
            $scopeName = $Scope
            $scopeKey = if ($Scope -eq 'repo') { $RepoKey } else { 'global' }
            if ($Scope -eq 'global') {
                # The opt-in direction is the one worth announcing: this memory
                # will surface on every repository, not just this one.
                Write-Host "GLOBAL scope: this memory will surface on every repository." -ForegroundColor Yellow
            }
            $a = @('-m', 'src.scripts.memory_store', '--scope', $scopeName, '--key', $scopeKey,
                   '--kind', $Kind, '--text-file', (New-TextFile -Text $Text),
                   '--confidence', $Confidence, '--importance', $Importance)
            if ($Topic) { $a += @('--topic', $Topic) }
            foreach ($t in $Tag) { $a += @('--tag', $t) }
            if ($Json) { $a += '--json' }
            & $Python @a
            $script:ExitCode = $LASTEXITCODE
            Show-Backlog
        }
        'extract' {
            $a = @('-m', 'src.scripts.memory_extract', '--repository', $RepoKey, '--since', $Since, '--root', $CallerDir)
            if ($Store) { $a += '--store' }
            if ($Json) { $a += '--json' }
            & $Python @a
            $script:ExitCode = $LASTEXITCODE
            Show-Backlog
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
    # `store` and `extract` capture their own exit code before Show-Backlog
    # runs, because Show-Backlog invokes python and would otherwise overwrite
    # $LASTEXITCODE - turning a queued write's exit 3 into a 0 that reads as
    # "stored". `health` computes its own.
    if ($Command -notin @('health', 'store', 'extract')) { $script:ExitCode = $LASTEXITCODE }
} finally {
    Pop-Location
}

exit $script:ExitCode
