# End-to-end test: what actually happens when people use this from real repos.
#
#   .\scripts\test-end-to-end.ps1
#   .\scripts\test-end-to-end.ps1 -Keep     # leave the fixtures for inspection
#
# Everything goes through ctx.ps1 - the same launcher a person or an agent uses -
# from directories that stand in for separate checkouts, so the repo scope key is
# derived exactly the way it is in real use rather than passed in.
#
# It walks the whole path a write takes:
#
#   server down          -> client spool on disk          (nothing lost)
#   server up, no embed  -> server queue, 202             (accepted, not searchable)
#   flush                -> client spool hands off to the server
#   read                 -> repo + global, isolated from other repos
#   drain                -> the queue empties and the memory becomes searchable
#
# The last two need a working embedder. If the provider is down or out of quota
# the script says so and reports those phases as BLOCKED - never as passed. A
# check that could not run is not a check that succeeded.

param(
    [switch]$Keep
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py = "$repo\.venv\Scripts\python.exe"
$ctx = "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1"
$spool = "$env:TEMP\ctx-e2e-spool"
$sandbox = "$env:TEMP\ctx-e2e-repos"

$results = @()
$blocked = @()

function Record($phase, $case, $expected, $actual, $pass) {
    $script:results += [pscustomobject]@{
        Phase = $phase; Case = $case; Expected = $expected; Actual = $actual; Pass = $pass
    }
    $mark = if ($pass) { 'PASS' } else { 'FAIL' }
    Write-Host ("  {0}  {1}" -f $mark, $case)
}

function Blocked($phase, $case, $why) {
    $script:blocked += [pscustomobject]@{ Phase = $phase; Case = $case; Why = $why }
    Write-Host ("  BLOCKED  {0}  - {1}" -f $case, $why) -ForegroundColor Yellow
}

function Run-Py($code) {
    # Written to a file rather than passed with -c: PowerShell 5.1 mangles
    # quotes inside a multi-line literal handed to a native executable, which
    # turned a tuple of strings into a syntax error.
    $f = Join-Path $env:TEMP ("ctx-e2e-{0}.py" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    [System.IO.File]::WriteAllText($f, $code, (New-Object System.Text.UTF8Encoding($false)))
    try { & $py $f } finally { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}

function Clear-ServerQueue {
    Push-Location "$repo\server"
    $f = Join-Path $env:TEMP 'ctx-e2e-clear.py'
    [System.IO.File]::WriteAllText($f, @"
from db import SessionLocal
from models import PendingMemory
with SessionLocal() as db:
    db.query(PendingMemory).delete(); db.commit()
"@, (New-Object System.Text.UTF8Encoding($false)))
    Get-Content $f | docker compose exec -T mem0 python - 2>&1 | Out-Null
    Remove-Item $f -Force -ErrorAction SilentlyContinue
    Pop-Location
}

function Stop-Server {
    Push-Location "$repo\server"; docker compose stop mem0 2>&1 | Out-Null; Pop-Location
}

function Start-Server {
    Push-Location "$repo\server"; docker compose start mem0 2>&1 | Out-Null; Pop-Location
    # curl blocks on the retries, so no sleep loop and no fixed wait.
    curl.exe -s --retry 40 --retry-connrefused --retry-delay 2 --retry-all-errors -o NUL "$liveUrl/docs" 2>&1 | Out-Null
}

function Invoke-Ctx {
    <#
      Run the launcher as its own process, the way a shell does.

      Calling it with `& $ctx` runs it in this session, where python writing its
      WARNING block to stderr becomes a NativeCommandError that aborts the whole
      run - the test died on the first store with no output. A child process
      also matches what actually happens when a person types the command.
    #>
    param([string[]]$CtxArgs)
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ctx @CtxArgs 2>&1 | Out-String
    return @{ Out = $out; Code = $LASTEXITCODE }
}

function Api-Get($path) {
    try {
        Invoke-RestMethod -Uri "$($env:MEM0_API_URL)$path" -Headers @{ 'X-API-Key' = $env:MEM0_API_KEY } -TimeoutSec 10
    } catch { $null }
}

function Spool-Count {
    if (-not (Test-Path $spool)) { return 0 }
    @(Get-ChildItem -Path $spool -Filter *.json -ErrorAction SilentlyContinue).Count
}

function Queue-Stats {
    $r = Api-Get '/memories/pending'
    if ($null -eq $r) { return @{ total = -1; pending = -1; error = -1 } }
    return @{ total = [int]$r.total; pending = [int]$r.pending; error = [int]$r.error;
              embedding = [int]$r.embedding; dead = [int]$r.dead; breaker = $r.circuit_breaker }
}

# --- setup ------------------------------------------------------------------

Remove-Item -Recurse -Force $spool, $sandbox -ErrorAction SilentlyContinue
$env:CONTEXT_MEMORY_SPOOL = $spool

# Two directories standing in for two checkouts. ctx.ps1 derives the repo scope
# key from the caller's directory before it does anything else, so this is the
# real derivation path, not a parameter override.
foreach ($r in 'e2e-repo-alpha', 'e2e-repo-beta') {
    New-Item -ItemType Directory -Force -Path "$sandbox\$r" | Out-Null
}

$envFile = "$repo\server\.env"
if (-not (Test-Path $envFile)) { Write-Error "server/.env not found"; exit 2 }
$adminKey = (Select-String -Path $envFile -Pattern '^ADMIN_API_KEY=(.*)$').Matches[0].Groups[1].Value
$apiPort = (Select-String -Path $envFile -Pattern '^MEM0_API_PORT=(.*)$').Matches[0].Groups[1].Value
if (-not $apiPort) { $apiPort = '8888' }
$liveUrl = "http://localhost:$apiPort"
$env:MEM0_API_KEY = $adminKey

# Clear the server queue so the counts below mean this run and not history.
Clear-ServerQueue

Write-Host ""
Write-Host "End-to-end: real prompts, from two repositories, two users" -ForegroundColor Cyan
Write-Host ""

# --- can the embedder actually embed right now? -----------------------------
# Decides whether the phases that need one run or are reported as blocked. A
# probe is cheap; guessing would let a whole outage read as a pass.
$env:MEM0_API_URL = $liveUrl
$env:MEM0_USER = 'e2e-probe'
$probeOut = & $py -m src.scripts.memory_store --scope task --key e2e-probe --kind note `
    --text 'Embedder probe for the end-to-end run.' 2>&1 | Out-String
$embedderWorks = ($probeOut -match 'stored ')
$blockReason = if ($embedderWorks) { '' } else {
    if ($probeOut -match 'quota exhausted') { 'the provider quota is spent' }
    elseif ($probeOut -match 'NOT searchable yet') { 'the server queued it - the embedder is refusing' }
    else { 'the embedder is unavailable' }
}
Write-Host ("embedder: {0}" -f $(if ($embedderWorks) { 'working' } else { "NOT working - $blockReason" })) `
    -ForegroundColor $(if ($embedderWorks) { 'Green' } else { 'Yellow' })
Write-Host ""

# --- Phase 1: the server is down. Nothing may be lost. ----------------------

# The probe write is itself queued when the embedder is refusing, so counts from
# here on are measured against a baseline rather than from zero. Assuming an
# empty queue would make every later total off by one, in a way that looks like
# a lost or duplicated write.
$baseQueue = (Queue-Stats).total

Write-Host "phase 1 - server down, writing from two repos and two users"
# The container is genuinely stopped rather than the URL pointed elsewhere.
# ctx.ps1 resolves the API port from server/.env and never from the
# environment, so an env override is silently ignored - the launcher would have
# kept talking to the live server while the test believed it was offline.
Stop-Server

$cases = @(
  @{ user = 'lautaro';  dir = 'e2e-repo-alpha'; scope = 'repo';   kind = 'discovery';
     text = 'Alpha pins node 20 because node 22 breaks the native addon.' }
  @{ user = 'lautaro';  dir = 'e2e-repo-beta';  scope = 'repo';   kind = 'decision';
     text = 'Beta stores uploads on S3 rather than the database after the 2GB row incident.' }
  @{ user = 'lautaro';  dir = 'e2e-repo-alpha'; scope = 'global'; kind = 'lesson';
     text = 'Piping a build into a pager replaces the exit code, so a failed build reports success.' }
  @{ user = 'teammate'; dir = 'e2e-repo-beta';  scope = 'repo';   kind = 'convention';
     text = 'Beta requires two approvals on anything touching the payments module.' }
)

foreach ($c in $cases) {
    $env:MEM0_USER = $c.user
    Push-Location "$sandbox\$($c.dir)"
    $rc = (Invoke-Ctx @('store', '-Text', $c.text, '-Kind', $c.kind, '-Scope', $c.scope)).Code
    Pop-Location
    Record 'server down' "$($c.user) in $($c.dir) -> $($c.scope)" 'exit 3, queued' "exit $rc" ($rc -eq 3)
}

$queued = Spool-Count
Record 'server down' 'every write is on disk' "$($cases.Count) spool files" "$queued files" ($queued -eq $cases.Count)

$entries = & $py -m src.scripts.memory_flush --list --json 2>$null | ConvertFrom-Json
$users = ($entries | ForEach-Object { $_.user } | Sort-Object -Unique) -join ','
Record 'server down' 'users are kept apart' 'lautaro,teammate' $users ($users -eq 'lautaro,teammate')

$scopes = ($entries | ForEach-Object { "$($_.scope):$($_.scope_key)" } | Sort-Object -Unique) -join ' '
$wantScopes = 'global:global repo:e2e-repo-alpha repo:e2e-repo-beta'
Record 'server down' 'the repo key came from the directory' $wantScopes $scopes ($scopes -eq $wantScopes)

# --- Phase 2: the server is up but cannot embed -----------------------------

Write-Host ""
Write-Host "phase 2 - server up, embedder refusing"
Start-Server
$env:MEM0_API_URL = $liveUrl
$env:MEM0_USER = 'lautaro'
$before = Spool-Count

Push-Location "$sandbox\e2e-repo-alpha"
$r = Invoke-Ctx @('store', '-Text', 'Alpha runs its integration suite against a disposable Postgres.', '-Kind', 'note')
$out = $r.Out; $rc = $r.Code
Pop-Location

$after = Spool-Count
$q = Queue-Stats

if ($embedderWorks) {
    Record 'server up' 'a normal write is stored outright' 'exit 0, "stored"' "exit $rc" (($rc -eq 0) -and ($out -match 'stored '))
} else {
    Record 'server up' 'the server accepts it into its own queue' 'exit 0, "accepted"' "exit $rc" (($rc -eq 0) -and ($out -match 'accepted '))
    Record 'server up' 'and says plainly it is not searchable yet' 'NOT searchable' `
        $(if ($out -match 'NOT searchable yet') { 'said so' } else { 'silent' }) ($out -match 'NOT searchable yet')
    Record 'server up' 'the server queue holds it' '>= 1 row' "$($q.total) rows" ($q.total -ge 1)
}

# A 202 counts as a success, so the auto-drain fires on it and hands the backlog
# over in the same command - the laptop empties the moment the server can take
# anything at all, without waiting for someone to run flush.
#
# But only THIS user's entries. Replaying a teammate's memory under our identity
# would file it against the wrong user_id and hide it from them, so theirs stay
# put until they write or someone flushes explicitly. Both halves are asserted:
# the drain happening, and the teammate's entry surviving it.
$mine = @($cases | Where-Object { $_.user -eq 'lautaro' }).Count
$theirs = $cases.Count - $mine

Record 'server up' "the accepted write drains this user's $mine spooled writes" `
    "$($cases.Count) spooled -> $theirs" "$after files" ($after -eq $theirs)
Record 'server up' "and leaves the teammate's $theirs alone" `
    "$theirs file(s) left" "$after files" ($after -eq $theirs)
Record 'server up' 'everything drained reached the server' `
    "queue = $($baseQueue + $mine + 1)" "$($q.total) rows" ($q.total -eq ($baseQueue + $mine + 1))

$teammateLeft = & $py -m src.scripts.memory_flush --list --json 2>$null | ConvertFrom-Json
$leftUsers = ($teammateLeft | ForEach-Object { $_.user } | Sort-Object -Unique) -join ','
Record 'server up' 'and the one left behind is the teammate''s' 'teammate' $leftUsers ($leftUsers -eq 'teammate')

# --- Phase 3: flush hands the client spool to the server --------------------

Write-Host ""
Write-Host "phase 3 - flush"
$beforeQueue = (Queue-Stats).total
$flushOut = & $py -m src.scripts.memory_flush 2>&1 | Out-String
$afterSpool = Spool-Count
$afterQueue = (Queue-Stats).total

# The explicit flush is what picks up what the per-user auto-drain could not:
# the teammate's entry. After it, nothing on this machine is still only local.
Record 'flush' "it replays the $theirs write the auto-drain left" "replayed $theirs" `
    $(if ($flushOut -match 'replayed') { 'replayed' } else { 'nothing' }) ($flushOut -match 'replayed')
Record 'flush' 'the spool is empty afterwards' '0 files' "$afterSpool files" ($afterSpool -eq 0)
Record 'flush' 'and the server has every write from both users' `
    "$($beforeQueue + $theirs) rows" "$afterQueue rows" ($afterQueue -eq ($beforeQueue + $theirs))

# --- Phase 4: reading back, and scope isolation -----------------------------

Write-Host ""
Write-Host "phase 4 - reading back"
if (-not $embedderWorks) {
    Blocked 'read' 'alpha sees its own memory' $blockReason
    Blocked 'read' 'alpha does NOT see beta''s memory' $blockReason
    Blocked 'read' 'the global memory is visible from both repos' $blockReason
} else {
    Push-Location "$sandbox\e2e-repo-alpha"
    $alpha = (Invoke-Ctx @('search', '-Query', 'which node version')).Out
    Pop-Location
    Record 'read' 'alpha sees its own memory' 'contains node 20' `
        $(if ($alpha -match 'node 20') { 'found' } else { 'MISSING' }) ($alpha -match 'node 20')
    Record 'read' 'alpha does NOT see beta''s memory' 'no S3 upload memory' `
        $(if ($alpha -match 'S3') { 'LEAKED' } else { 'isolated' }) (-not ($alpha -match 'S3'))
    Record 'read' 'the global memory is visible from alpha' 'contains pager' `
        $(if ($alpha -match 'pager') { 'found' } else { 'MISSING' }) ($alpha -match 'pager')
}

# --- Phase 5: the queue drains and the memory becomes searchable ------------

Write-Host ""
Write-Host "phase 5 - draining"
if (-not $embedderWorks) {
    Blocked 'drain' 'the server queue empties' $blockReason
    Blocked 'drain' 'a queued memory becomes searchable' $blockReason
} else {
    & $py -m src.scripts.memory_flush --force *>$null
    Start-Sleep -Seconds 3
    $q = Queue-Stats
    Record 'drain' 'the server queue empties' '0 rows' "$($q.total) rows" ($q.total -eq 0)
}

# --- Phase 6: is any of this visible? ---------------------------------------

Write-Host ""
Write-Host "phase 6 - visibility"
$q = Queue-Stats
Push-Location "$sandbox\e2e-repo-alpha"
$h = Invoke-Ctx @('health')
$health = $h.Out; $healthRc = $h.Code
Pop-Location

$unstored = (Spool-Count) + $q.total
Record 'visibility' 'health reports both queues' 'spool + server queue' `
    $(if (($health -match 'spool:') -and ($health -match 'server queue:')) { 'both shown' } else { 'incomplete' }) `
    (($health -match 'spool:') -and ($health -match 'server queue:'))

if ($unstored -gt 0) {
    Record 'visibility' 'health exits non-zero while anything is unsearchable' 'exit 1' "exit $healthRc" ($healthRc -ne 0)
} else {
    Record 'visibility' 'health exits zero when everything is stored' 'exit 0' "exit $healthRc" ($healthRc -eq 0)
}

if (-not $embedderWorks) {
    Record 'visibility' 'the parked breaker is reported' 'open' `
        $(if ($q.breaker.open) { 'open' } else { 'not open' }) ([bool]$q.breaker.open)
}

# --- report -----------------------------------------------------------------

Write-Host ""
Write-Host "================ RESULTS ================" -ForegroundColor Cyan
$results | Format-Table Phase, Case, Expected, Actual -AutoSize | Out-String -Width 200 | Write-Host

$passed = @($results | Where-Object { $_.Pass }).Count
$total = $results.Count
Write-Host ("{0} of {1} checks passed" -f $passed, $total) -ForegroundColor $(if ($passed -eq $total) { 'Green' } else { 'Red' })

if ($blocked.Count) {
    Write-Host ""
    Write-Host ("{0} check(s) COULD NOT RUN - these are not passes:" -f $blocked.Count) -ForegroundColor Yellow
    foreach ($b in $blocked) { Write-Host ("  - [{0}] {1}  ({2})" -f $b.Phase, $b.Case, $b.Why) -ForegroundColor Yellow }
    Write-Host "  Re-run once the embedder is available to close them." -ForegroundColor Yellow
}

# --- cleanup ----------------------------------------------------------------

if ($Keep) {
    Write-Host ""
    Write-Host "-Keep set: fixtures left in $spool and $sandbox, and in the server queue."
} else {
    Remove-Item -Recurse -Force $spool, $sandbox -ErrorAction SilentlyContinue
    Clear-ServerQueue
    # Cleans by matching the fixture text only, never by scope: an earlier
    # version of a sibling script cleaned by scope and destroyed the real global
    # memories.
    Run-Py @"
import os, sys
sys.path.insert(0, os.getcwd())
from src.memory.mem0_provider import Mem0Provider
from src.memory.scopes import Scope
marks = ('Alpha pins node 20', 'Beta stores uploads on S3', 'Piping a build into a pager',
         'Beta requires two approvals', 'Alpha runs its integration suite',
         'Embedder probe for the end-to-end run')
try:
    p = Mem0Provider.from_env()
except Exception:
    raise SystemExit(0)
removed = 0
for scope, key in ((Scope.REPOSITORY, 'e2e-repo-alpha'), (Scope.REPOSITORY, 'e2e-repo-beta'),
                   (Scope.GLOBAL, 'global'), (Scope.TASK, 'e2e-probe')):
    try:
        page = p.get_all(scope=scope, scope_key=key, top_k=200)
    except Exception:
        continue
    for r in page.records:
        if any(m in (r.text or '') for m in marks):
            try:
                p.delete(r.id); removed += 1
            except Exception:
                pass
p.close()
print('removed %d fixture memories' % removed)
"@
}

exit $(if ($passed -eq $total) { 0 } else { 1 })
