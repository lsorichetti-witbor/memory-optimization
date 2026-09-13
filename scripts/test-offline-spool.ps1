# Offline-spool end-to-end test: multiple users x multiple repositories x shared.
#
# Run from anywhere:  .\scripts	est-offline-spool.ps1
# Requires the stack running and server/.env present.
#
# Isolation: it uses its own CONTEXT_MEMORY_SPOOL and cleans up BY TOPIC, never
# by scope. An earlier version cleaned by scope and destroyed the real shared
# memories, which is the whole reason the cleanup is written the way it is.
param(
    # Leave the fixtures in the store instead of cleaning up. Use it when you
    # want to SEE the multi-user / multi-repository result in the dashboard
    # rather than only read that the checks passed. Re-run without -Keep to
    # remove them again.
    [switch]$Keep
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py = "$repo\.venv\Scripts\python.exe"
$spool = "$env:TEMP\ctx-offline-test-spool"
$results = @()

function Record($phase, $case, $expected, $actual, $pass) {
    $script:results += [pscustomobject]@{ Phase = $phase; Case = $case; Expected = $expected; Actual = $actual; Pass = $pass }
}

Remove-Item -Recurse -Force $spool -ErrorAction SilentlyContinue
$env:CONTEXT_MEMORY_SPOOL = $spool
Set-Location $repo

$env:MEM0_API_KEY = (Select-String -Path "server\.env" -Pattern '^ADMIN_API_KEY=(.*)$').Matches[0].Groups[1].Value

# ---- Phase 1: server DOWN. Point at a dead port; nothing must be lost. ----
$env:MEM0_API_URL = "http://localhost:59999"

$cases = @(
  @{ user='lautaro';  scope='repository'; key='repo-alpha'; kind='discovery'; topic='alpha.build';  text='Repo alpha pins its build to node 20 because node 22 breaks the native addon.' }
  @{ user='lautaro';  scope='repository'; key='repo-beta';  kind='decision';  topic='beta.storage'; text='Repo beta stores uploads on S3 rather than the database after the 2GB row incident.' }
  @{ user='lautaro';  scope='global';     key='global';     kind='lesson';    topic='shared.exitcodes'; text='Piping a build into a pager replaces the exit code with the pager exit code, so a failed build reports success.' }
  @{ user='teammate'; scope='repository'; key='repo-alpha'; kind='convention';topic='alpha.reviews'; text='Repo alpha requires two approvals on anything touching the payments module.' }
  @{ user='teammate'; scope='global';     key='global';     kind='discovery'; topic='shared.tls';   text='Antivirus HTTPS scanning re-signs certificates, so package managers fail with CERTIFICATE_VERIFY_FAILED until the root is trusted.' }
)

foreach ($c in $cases) {
    $env:MEM0_USER = $c.user
    $env:MEM0_REPOSITORY = if ($c.scope -eq 'repository') { $c.key } else { $null }
    & $py -m src.scripts.memory_store --scope $c.scope --key $c.key --kind $c.kind --topic $c.topic --text $c.text 2>&1 | Out-Null
    $rc = $LASTEXITCODE
    Record 'server down' "$($c.user) -> $($c.scope):$($c.key)" 'exit 3 (queued, not lost)' "exit $rc" ($rc -eq 3)
}

$queued = (Get-ChildItem "$spool\*.json" -ErrorAction SilentlyContinue).Count
Record 'server down' 'spool file count' "$($cases.Count) files" "$queued files" ($queued -eq $cases.Count)

# every scope and user present in ONE shared spool
$payloads = Get-ChildItem "$spool\*.json" | ForEach-Object { Get-Content $_ -Raw | ConvertFrom-Json }
$users = ($payloads.user | Sort-Object -Unique) -join ','
Record 'server down' 'users kept apart' 'lautaro,teammate' $users ($users -eq 'lautaro,teammate')
$scopes = ($payloads | ForEach-Object { "$($_.scope):$($_.scope_key)" } | Sort-Object -Unique) -join ' '
$expectScopes = 'global:global repository:repo-alpha repository:repo-beta'
Record 'server down' 'all repos + shared in one spool' $expectScopes $scopes ($scopes -eq $expectScopes)
$withStamp = ($payloads | Where-Object { $_.created_at }).Count
Record 'server down' 'every entry timestamped' "$($cases.Count)" "$withStamp" ($withStamp -eq $cases.Count)

# ---- Phase 2: server UP. Replay, verify, and confirm the queue empties. ----
$env:MEM0_API_URL = "http://localhost:8888"
$env:MEM0_USER = 'lautaro'

$listOut = & $py -m src.scripts.memory_flush --list 2>&1 | Out-String
Record 'server up' 'list shows the queue before replay' "$($cases.Count) write(s) queued" (($listOut -split "`n")[0].Trim()) ($listOut -match "$($cases.Count) write\(s\) queued")

$flushOut = & $py -m src.scripts.memory_flush 2>&1 | Out-String
$flushRc = $LASTEXITCODE
Record 'server up' 'flush exit code' 'exit 0 (nothing left queued)' "exit $flushRc" ($flushRc -eq 0)
Record 'server up' 'flush report' "$($cases.Count) of $($cases.Count) replayed" ($flushOut.Trim() -split "`n")[0] ($flushOut -match "$($cases.Count) of $($cases.Count) spooled writes replayed")

$left = (Get-ChildItem "$spool\*.json" -ErrorAction SilentlyContinue).Count
Record 'server up' 'spool emptied after success' '0 files' "$left files" ($left -eq 0)

# ---- Phase 3: the replayed memories are readable, per user and per scope ----
function Probe($user, $scope, $key, $needle) {
    $env:MEM0_USER = $user
    $out = & $py -m src.scripts.memory_search --scope $scope --key $key --query $needle --top-k 10 2>&1 | Out-String
    return $out
}

$p = Probe 'lautaro' 'repository' 'repo-alpha' 'node 20 native addon'
Record 'readback' 'lautaro sees repo-alpha' 'contains "node 20"' $(if ($p -match 'node 20') {'found'} else {'MISSING'}) ($p -match 'node 20')

$p = Probe 'lautaro' 'repository' 'repo-beta' 'uploads storage S3'
Record 'readback' 'lautaro sees repo-beta' 'contains "S3"' $(if ($p -match 'S3') {'found'} else {'MISSING'}) ($p -match 'S3')

$p = Probe 'lautaro' 'repository' 'repo-alpha' 'uploads storage S3'
Record 'readback' 'repo-beta memory NOT in repo-alpha' 'no "2GB row incident"' $(if ($p -match '2GB row incident') {'LEAKED'} else {'isolated'}) (-not ($p -match '2GB row incident'))

$p = Probe 'lautaro' 'global' 'global' 'exit code pager build'
Record 'readback' 'lautaro sees shared scope' 'contains "pager"' $(if ($p -match 'pager') {'found'} else {'MISSING'}) ($p -match 'pager')

$p = Probe 'teammate' 'repository' 'repo-alpha' 'approvals payments module'
Record 'readback' 'teammate sees own repo-alpha memory' 'contains "two approvals"' $(if ($p -match 'two approvals') {'found'} else {'MISSING'}) ($p -match 'two approvals')

$p = Probe 'teammate' 'repository' 'repo-alpha' 'node 20 native addon'
Record 'readback' "teammate does NOT see lautaro's memory" 'no "native addon"' $(if ($p -match 'native addon') {'LEAKED'} else {'isolated'}) (-not ($p -match 'native addon'))

$p = Probe 'teammate' 'global' 'global' 'certificate scanning antivirus'
Record 'readback' 'teammate sees own shared memory' 'contains "CERTIFICATE_VERIFY_FAILED"' $(if ($p -match 'CERTIFICATE_VERIFY_FAILED') {'found'} else {'MISSING'}) ($p -match 'CERTIFICATE_VERIFY_FAILED')

# ---- Phase 4: partial failure must keep the file ----
$env:MEM0_USER = 'lautaro'
$env:MEM0_API_URL = "http://localhost:59999"
& $py -m src.scripts.memory_store --scope repository --key repo-gamma --kind note --text "A memory queued for a failure test, long enough to be stored." 2>&1 | Out-Null
$env:MEM0_API_URL = "http://localhost:59998"   # still down on flush
$out = & $py -m src.scripts.memory_flush 2>&1 | Out-String
$rc = $LASTEXITCODE
$stillThere = (Get-ChildItem "$spool\*.json" -ErrorAction SilentlyContinue).Count
Record 'failed replay' 'flush exit code when it cannot replay' 'exit 1' "exit $rc" ($rc -eq 1)
Record 'failed replay' 'file kept, not deleted' '1 file' "$stillThere files" ($stillThere -eq 1)

# recover it so the test leaves nothing behind
$env:MEM0_API_URL = "http://localhost:8888"
& $py -m src.scripts.memory_flush 2>&1 | Out-Null
$final = (Get-ChildItem "$spool\*.json" -ErrorAction SilentlyContinue).Count
Record 'failed replay' 'recovers once reachable' '0 files' "$final files" ($final -eq 0)

# ---- cleanup: remove ONLY what this test created ----
if ($Keep) {
    ""
    "-Keep set: the fixtures were left in place for inspection."
    "  users:        lautaro, teammate"
    "  repositories: repo-alpha, repo-beta, repo-gamma"
    "  shared scope: global"
    "  Re-run without -Keep to remove them."
} else {
# By topic, not by scope. The shared scope holds real memories, and deleting the
# whole scope to tidy up a test would destroy them - which is exactly what an
# earlier version of this script did.
& $py -c "import os,sys; sys.path.insert(0,'.'); from src.memory.mem0_provider import Mem0Provider; from src.memory.scopes import Scope
TOPICS={'alpha.build','beta.storage','shared.exitcodes','alpha.reviews','shared.tls'}
removed=0
for u in ('lautaro','teammate'):
    os.environ['MEM0_USER']=u
    p=Mem0Provider.from_env()
    for sc,k in ((Scope.REPOSITORY,'repo-alpha'),(Scope.REPOSITORY,'repo-beta'),(Scope.REPOSITORY,'repo-gamma'),(Scope.GLOBAL,'global')):
        try:
            for r in p.get_all(scope=sc, scope_key=k, top_k=200).records:
                if r.envelope.topic in TOPICS or 'queued for a failure test' in r.text:
                    p.delete(r.id); removed+=1
        except Exception: pass
    p.close()
print('cleanup done, removed', removed)" 2>&1 | Select-Object -Last 1
}

Remove-Item -Recurse -Force $spool -ErrorAction SilentlyContinue
Remove-Item Env:\CONTEXT_MEMORY_SPOOL -ErrorAction SilentlyContinue

# ---- report ----
""
"================ RESULTS ================"
$results | Format-Table -AutoSize -Wrap
$passed = ($results | Where-Object { $_.Pass }).Count
"$passed of $($results.Count) checks passed"
$results | ConvertTo-Json -Depth 3 | Set-Content "$env:TEMP\ctx-offline-results.json" -Encoding UTF8
