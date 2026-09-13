# Context Memory — local deployment guide

How to bring the self-hosted Mem0 stack and the Context Manager up from a fresh
checkout, and what to do when it does not work.

Every command here was run on Windows 11 / PowerShell 5.1 / Docker Desktop
29.7.2 / Python 3.11.4. Where a step differs on another platform it says so.

The troubleshooting section at the end is not hypothetical: every entry is a
failure that actually happened during the build, with the symptom as it was
observed. Most of them fail *quietly*, which is why they are written down.

---

## 0. What you are deploying

```
ctx.ps1  ──►  src/context  ──►  four sources: instructions, current state,
(launcher)    (selection)        repository, memory
                   │
                   └──►  src/memory  ──►  Mem0 REST API  ──►  pgvector
                         (long-term)      (docker)           (docker)
```

Three containers: `mem0` (FastAPI), `postgres` (pgvector), `mem0-dashboard`
(Next.js, optional). One Python venv for the Context Manager. One skill folder
in `~/.claude/skills/`.

## 1. Prerequisites

| Need | Version used | Check |
|---|---|---|
| Docker Desktop | 29.7.2, Compose v5.4.0 | `docker compose version` |
| Python | 3.11.4 | `python --version` |
| Git | any | `git --version` |
| A Gemini API key | — | https://aistudio.google.com/apikey |

**On the LLM provider.** `server/main.py` bundles `openai`, `anthropic` and
`gemini`. Only `openai` and `gemini` can do embeddings — Anthropic ships no
embedding model — so with an Anthropic key alone you cannot run the stack.
Gemini covers both legs and is what this guide uses.

## 2. Create `server/.env`

Gitignored, so it never exists in a fresh clone. Generate the secrets rather
than inventing them:

```powershell
cd "<checkout>\server"
$rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
function New-Secret([int]$n) { $b = New-Object byte[] $n; $rng.GetBytes($b); [Convert]::ToBase64String($b) }
$pg    = (New-Secret 32) -replace '[^A-Za-z0-9]',''
$admin = (New-Secret 48) -replace '[^A-Za-z0-9]',''
$jwt   = New-Secret 48
if ($pg -match '^A+$') { throw "RNG produced a degenerate secret" }   # see troubleshooting
```

> PowerShell 5.1 has no `RandomNumberGenerator::Fill`. Using it silently leaves
> the byte array all zeros and the "secret" becomes a run of `A`s. The guard
> above exists because that happened.

Then copy `.env.example` to `.env` and fill in:

```
GOOGLE_API_KEY=<your key>
POSTGRES_PASSWORD=<$pg>
ADMIN_API_KEY=<$admin>
JWT_SECRET=<$jwt>

MEM0_USER=<your identity, e.g. lautaro>

MEM0_DEFAULT_LLM_PROVIDER=gemini
MEM0_DEFAULT_EMBEDDER_PROVIDER=gemini
MEM0_DEFAULT_LLM_MODEL=gemini-3.6-flash
MEM0_DEFAULT_EMBEDDER_MODEL=models/gemini-embedding-001
MEM0_EMBEDDING_DIMS=768

MEM0_API_PORT=8888
POSTGRES_HOST_PORT=8432
DASHBOARD_PORT=3000
```

Three of these are easy to get wrong and fail late:

- **`MEM0_EMBEDDING_DIMS` must match the embedder.** pgvector creates the column
  at this width; `models/gemini-embedding-001` emits 768, OpenAI's
  `text-embedding-3-small` emits 1536. A mismatch fails at the first insert,
  after the table exists.
- **`MEM0_DEFAULT_LLM_MODEL` goes stale.** Gemini retires model ids. Confirm
  with `models.list()` (step 6) rather than trusting a doc or an error message.
- **`MEM0_USER` is part of the search filter.** Change it later and every memory
  written under the old value becomes invisible — the search returns nothing,
  which looks exactly like an empty store.

## 3. Optional: TLS-interception certificate

Skip unless your network re-signs HTTPS (corporate proxy, or antivirus with
HTTPS scanning — Norton, Kaspersky, ESET). Symptom: `pip` and `pnpm` fail with
`CERTIFICATE_VERIFY_FAILED` or `unable to verify the first certificate`.

Check what is actually presented:

```powershell
$c = New-Object System.Net.Sockets.TcpClient("pypi.org", 443)
$ssl = New-Object System.Net.Security.SslStream($c.GetStream(), $false, ({$true} -as [Net.Security.RemoteCertificateValidationCallback]))
$ssl.AuthenticateAsClient("pypi.org")
(New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)).Issuer
$ssl.Close(); $c.Close()
```

A real chain names a public CA. Anything else is interception. Export that root
from the Windows store into **both** cert directories:

```powershell
$certs = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
         Where-Object { $_.Issuer -eq $_.Subject -and $_.Subject -like "*<your interceptor>*" } |
         Select-Object -Unique
foreach ($dir in "<checkout>\server\certs", "<checkout>\server\dashboard\certs") {
  $lines = foreach ($c in $certs) {
    "-----BEGIN CERTIFICATE-----"
    [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
    "-----END CERTIFICATE-----"
  }
  Set-Content -Path "$dir\local-tls-interception.crt" -Value $lines -Encoding ascii
}
```

Both `certs/` directories ship empty and the `.crt` is gitignored, so this is a
no-op on a normal network. One file per certificate, deduplicated — the Windows
store often holds the same root twice.

For the host `pip`, point it at a bundle on an **ASCII-only path**:

```powershell
$bundle = "$env:LOCALAPPDATA\pip\ca-bundle.pem"
New-Item -ItemType Directory -Force (Split-Path $bundle) | Out-Null
Get-Content "<checkout>\.venv\Lib\site-packages\pip\_vendor\certifi\cacert.pem",
            "<checkout>\server\certs\local-tls-interception.crt" | Set-Content $bundle -Encoding ascii
[System.IO.File]::WriteAllText("<checkout>\.venv\pip.ini", "[global]`r`ncert = $bundle`r`n",
                               (New-Object System.Text.ASCIIEncoding))
```

The ASCII path is not fussiness: pip 23.1.2 misreads a non-ASCII path out of its
own config file, and this repo's path contains `Gestión`.

## 4. Start the stack

```powershell
cd "<checkout>"
.\scripts\stack.ps1 up
```

Or directly, which is what the script does:

```powershell
cd "<checkout>\server"
docker compose up -d --build
```

The Makefile is **not** usable on Windows: `server/Makefile:9` calls `lsof`.

First build pulls a ~236 MB base image and installs both Python and Node
dependencies. Expect several minutes, and see the Docker engine entry in
troubleshooting if it dies partway.

Skip the dashboard if you only want the API:

```powershell
docker compose up -d --build mem0 postgres
```

## 5. Create the Python environment

```powershell
cd "<checkout>"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-context.txt
```

`tiktoken` is deliberately absent. Token counting falls back to a ~4 chars/token
heuristic and every budget report names the counter it used, rather than
implying a precision it does not have. Install `tiktoken` if you want exact
OpenAI-tokenizer counts.

## 6. Verify — in this order

Each step catches a different failure. Do not skip ahead.

```powershell
# 1. containers
.\scripts\stack.ps1 health
#    expect: API 200, Dashboard 200, Postgres ok

# 2. the database the API needs actually exists
cd server; docker compose exec -T postgres psql -U postgres -tAc "SELECT datname FROM pg_database ORDER BY datname"; cd ..
#    expect: mem0_app, postgres, template0, template1
#    missing mem0_app => init-db.sh did not run, see troubleshooting

# 3. the API's own spec
curl.exe -fsS http://localhost:8888/openapi.json > $null; "openapi ok"

# 4. the EMBEDDER leg
$env:MEM0_API_URL='http://localhost:8888'; $env:MEM0_USER='<your MEM0_USER>'
$env:MEM0_API_KEY=(Select-String -Path server\.env -Pattern '^ADMIN_API_KEY=(.*)$').Matches[0].Groups[1].Value
.\.venv\Scripts\python.exe -m src.scripts.memory_store --scope task --key smoke --kind note --text "A smoke test memory long enough to be stored."

# 5. the LLM leg — a DIFFERENT code path, and the one that breaks on a retired model
.\.venv\Scripts\python.exe -m src.scripts.memory_store --scope task --key smoke --kind note --infer --text "We chose Gemini because Anthropic ships no embedding model."

# 6. clean up the smoke memories
.\.venv\Scripts\python.exe -c "import os,sys; sys.path.insert(0,'.'); from src.memory.mem0_provider import Mem0Provider; from src.memory.scopes import Scope; p=Mem0Provider.from_env(); p.delete_all(scope=Scope.TASK, scope_key='smoke'); print('cleaned')"

# 7. the test suite
.\.venv\Scripts\python.exe -m pytest tests/context_memory -q
#    expect: 229 passed. A SKIP on the integration tests means the env vars did
#    not reach pytest - that is a failure of this step, not a pass.
```

**Steps 4 and 5 are not redundant.** `--infer` is the only one that calls the
LLM; without it the embedder alone runs. A green suite said nothing about the
LLM path while `gemini-2.0-flash` was retired and every extraction 502'd.

To list models the key can actually use:

```powershell
cd server
docker compose exec -T mem0 python -c "import os; from google import genai; c=genai.Client(api_key=os.environ['GOOGLE_API_KEY']); print([m.name for m in c.models.list() if 'generateContent' in (getattr(m,'supported_actions',None) or [])])"
```

## 7. Install the skills

```powershell
$src = "<checkout>\skills"; $dst = "$env:USERPROFILE\.claude\skills"
foreach ($s in 'context-memory','mem0','mem0-integrate','mem0-test-integration') {
  if (Test-Path "$dst\$s") { Remove-Item -Recurse -Force "$dst\$s" }
  Copy-Item -Recurse "$src\$s" "$dst\$s"
}
# pin the checkout for the launcher - generated, not in git
[System.IO.File]::WriteAllText("$dst\context-memory\scripts\home.txt", "<checkout>`r`n",
                               (New-Object System.Text.UTF8Encoding($true)))
& "$dst\context-memory\scripts\ctx.ps1" health
```

Optional: `mem0-cli`, `mem0-vercel-ai-sdk`.

**Do not install `mem0-oss-to-platform`.** It migrates projects *off* self-hosted
onto the hosted Platform, and its own trigger fires "even when the user doesn't
say the word migrate".

`skills/AGENTS.md`, `skills/CLAUDE.md` and `skills/README.md` are contributor
docs about that directory, not skills. Only `<name>/SKILL.md` folders load.

## 8. Daily use

```powershell
$ctx = "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1"
& $ctx health
& $ctx build  -Task "..." -ReportOnly
& $ctx search -Query "..."
& $ctx store  -Scope repository|global -Kind discovery -Topic <topic> -Text "..."
```

See `skills/context-memory/SKILL.md` for the full contract.

---

## Troubleshooting

Ordered by how quietly each one fails.

### The stack starts, then the API says `database "mem0_app" does not exist`

`server/init-db.sh` was checked out with CRLF. The kernel looks for an
interpreter named `/bin/bash\r`, and postgres logs
`cannot execute: required file not found` — which reads as a missing file.

**Postgres still reports healthy**, so `docker compose ps` looks fine and the
only symptom arrives later from the API.

`.gitattributes` now pins `*.sh text eol=lf`, so a fresh clone is correct. An
existing checkout needs re-normalising, and the volume must be recreated because
init scripts only run on first initialisation:

```powershell
git rm --cached -r . ; git reset --hard      # re-checkout with correct endings
cd server; docker compose down -v; docker compose up -d postgres
```

`down -v` deletes every stored memory.

### A search returns nothing, and nothing is wrong

Three silent causes, in order of likelihood:

1. **`MEM0_USER` differs** from the value the memories were written under. It is
   part of the search filter, so the result is empty rather than an error.
2. **The repository scope key differs.** `ctx.ps1` derives it from the git
   remote; a clone with no remote falls back to the directory name.
3. There genuinely is nothing stored.

`& $ctx health` prints all three inputs. `& $ctx search` also prints this
warning itself when both scopes come back empty.

### A search returns results that are obviously irrelevant

Similarity search always returns nearest neighbours. Measured: a nonsense query
scored 0.50–0.55 against real memories scoring 0.65–0.78. A result is not
evidence of a match — its score is. Use `-Threshold`, with a number derived from
your own data.

### `pip install` fails with `CERTIFICATE_VERIFY_FAILED`

TLS interception. See step 3. Inside the Docker build the same failure appears
as `Could not find a version that satisfies the requirement fastapi`.

### The dashboard build fails on `pnpm i`

Same cause, but the fix order is not obvious and each part is required:

1. `apk` needs the cert to reach its own package index, so the bundle is seeded
   by hand **before** any `apk add`. Installing `ca-certificates` first cannot
   work — that install is the thing that needs the cert.
2. `update-ca-certificates` then regenerates the bundle and does **not** recurse
   into a subdirectory, silently dropping what step 1 added. Measured: 119 certs
   with the local root gone, `apk` working, every Node download still failing.
3. Node ignores the system trust store entirely; `NODE_EXTRA_CA_CERTS` is
   required on top.

All three are in `server/dashboard/Dockerfile`. If you edit it, rebuild with
`--no-cache`: a cached `COPY certs/` layer produced an image with the `.crt`
missing while the same build without cache had it.

### `rpc error: code = Unavailable desc = error reading from server: EOF`

The Docker Desktop engine died mid-build; afterwards the API often returns 500.
Observed five times on one machine with 15.7 GB RAM, 6.1 GB free and 257 GB free
disk — it is not resource pressure. Restart Docker Desktop and rebuild; cached
layers make the retry short.

```powershell
Get-Process "Docker Desktop","com.docker.backend" -EA SilentlyContinue | Stop-Process -Force
Start-Sleep 8; Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
```

**Do not pipe a build into `tail` or `Select-Object` to check success.** The exit
code then comes from the pager, not from docker. A build that had failed this way
reported exit 0.

### `POST /memories` returns 502

Read the container log, not the status code:

```powershell
cd server; docker compose logs mem0 --since 5m | Select-String -Pattern "Error|error:" | Select-Object -Last 5
```

Most likely a retired Gemini model id (`gemini-2.0-flash` now 404s). Confirm
against `models.list()` — the id suggested in the error text is not guaranteed
to be available to your key.

### `stack.ps1` reports the venv missing, with a mangled path

PowerShell 5.1 reads `.ps1` files as ANSI unless they carry a UTF-8 BOM. With a
non-ASCII path in the checkout, `Gestión` decodes as `GestiÃ³n`. `ctx.ps1` ships
with a BOM and derives its path from `home.txt`; if you write your own wrapper,
save it as UTF-8 **with** BOM.

### Port already in use

Change `MEM0_API_PORT` / `POSTGRES_HOST_PORT` / `DASHBOARD_PORT` in `server/.env`.
Both compose and `stack.ps1` read them from there, so they cannot disagree.

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8888
```

### A context build raises `BudgetExceeded`

Pinned instruction files alone exceed the budget. This is deliberate — policy is
never trimmed silently — but it usually means too much is being treated as
policy. Only instruction files on the task's own path are pinned. Raise
`-MaxTokens`, or check for an unexpected `CLAUDE.md` / `AGENTS.md` near the task.
