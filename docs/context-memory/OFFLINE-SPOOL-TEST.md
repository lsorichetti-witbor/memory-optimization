# Offline spool — design, test and result

**Date:** 2026-09-13 · **Result:** 23 of 23 checks passed · **Harness:**
`scripts/test-offline-spool.ps1` (re-runnable)

---

## 1. What was built

A memory is worth writing at the moment you notice it. If the stack happens to
be down and the command merely errors, the observation is gone — so an
unreachable server **queues** instead of failing.

```
store  ──►  server reachable?  ──yes──►  stored
                   │
                   no
                   ▼
           WARNING + queued to the spool (exit 3)
                   │
        ... later, server back up ...
                   │
                   ▼
           flush  ──►  replay oldest-first  ──►  delete file ONLY on success
```

One spool serves **every repository, the shared scope, and every user on the
machine**. A per-repo queue would strand a memory in whichever checkout you
happened to be in, and you would never think to look there. Entries carry their
own scope, scope key, user and timestamp, so one directory is enough.

Location: `%LOCALAPPDATA%\context-memory\spool` (override: `CONTEXT_MEMORY_SPOOL`).
One JSON file per write, named `<UTC timestamp>-<random>.json` so lexical order
is chronological order.

### Three properties, each because the alternative fails quietly

| Property | What it prevents |
|---|---|
| **Original timestamp survives the replay** | A month-old memory replayed today would otherwise rank as brand new. Recency is a ranking signal. |
| **Nothing is deleted until confirmed stored** | Deleting on failure destroys the only copy, and the next run just shows an empty spool. |
| **Only transport failures queue** | A `400` means the *write* is wrong. Queueing it would retry a failure forever, so it fails now instead. |

The filename also carries a random suffix: two writes in the same second would
otherwise overwrite each other, and the loss would be silent.

### Surfacing it

A queued write is invisible unless something says so, so `ctx.ps1 health` always
prints the pending count and warns when it is non-zero:

```
spool:            1 queued  (C:\Users\...\context-memory\spool)

1 memory write(s) are queued and NOT stored. Replay them with:
  & 'C:\Users\Witbor\.claude\skills\context-memory\scripts\ctx.ps1' flush
```

---

## 2. The test

`scripts/test-offline-spool.ps1`. Two users × three repositories × the shared
scope, across four phases. It uses an isolated `CONTEXT_MEMORY_SPOOL` so it can
never touch the real queue.

**Fixtures** — deliberately distinct content per user and scope, so a leak
between them is detectable by substring rather than by count:

| # | User | Scope | Key | Topic |
|---|---|---|---|---|
| 1 | lautaro | repository | repo-alpha | `alpha.build` |
| 2 | lautaro | repository | repo-beta | `beta.storage` |
| 3 | lautaro | global | global | `shared.exitcodes` |
| 4 | teammate | repository | repo-alpha | `alpha.reviews` |
| 5 | teammate | global | global | `shared.tls` |

"Server down" is simulated by pointing `MEM0_API_URL` at a closed port
(`localhost:59999`) — a real connection refusal, not a mock.

---

## 3. Results — 23 of 23

### Phase 1 · server down (9 checks)

| Check | Expected | Actual | |
|---|---|---|---|
| lautaro → repository:repo-alpha | exit 3, queued | exit 3 | PASS |
| lautaro → repository:repo-beta | exit 3, queued | exit 3 | PASS |
| lautaro → global:global | exit 3, queued | exit 3 | PASS |
| teammate → repository:repo-alpha | exit 3, queued | exit 3 | PASS |
| teammate → global:global | exit 3, queued | exit 3 | PASS |
| spool file count | 5 files | 5 files | PASS |
| users kept apart | `lautaro,teammate` | `lautaro,teammate` | PASS |
| all repos + shared in one spool | `global:global repository:repo-alpha repository:repo-beta` | identical | PASS |
| every entry timestamped | 5 | 5 | PASS |

Exit code **3** is distinct from `1` (bad write) and `2` (bad configuration), so
a caller can tell "queued, try later" from "this will never work".

### Phase 2 · server up, replay (4 checks)

| Check | Expected | Actual | |
|---|---|---|---|
| `--list` shows the queue before replay | 5 write(s) queued | 5 write(s) queued | PASS |
| flush exit code | 0 | 0 | PASS |
| flush report | 5 of 5 replayed | `5 of 5 spooled writes replayed, 0 still queued` | PASS |
| spool emptied after success | 0 files | 0 files | PASS |

### Phase 3 · read-back and isolation (7 checks)

The point of the whole exercise: the replayed memories land in the right scope,
under the right user, and nowhere else.

| Check | Expected | Actual | |
|---|---|---|---|
| lautaro sees repo-alpha | contains "node 20" | found | PASS |
| lautaro sees repo-beta | contains "S3" | found | PASS |
| **repo-beta memory NOT in repo-alpha** | no "2GB row incident" | isolated | PASS |
| lautaro sees shared scope | contains "pager" | found | PASS |
| teammate sees own repo-alpha memory | contains "two approvals" | found | PASS |
| **teammate does NOT see lautaro's memory** | no "native addon" | isolated | PASS |
| teammate sees own shared memory | contains "CERTIFICATE_VERIFY_FAILED" | found | PASS |

Both users wrote into `repo-alpha`, and each sees only their own — `user_id` is
part of the filter, which is also why each entry is replayed **as its own
author** rather than as whoever ran the flush.

### Phase 4 · failed replay (3 checks)

| Check | Expected | Actual | |
|---|---|---|---|
| flush exit code when it cannot replay | 1 | 1 | PASS |
| file kept, not deleted | 1 file | 1 file | PASS |
| recovers once reachable | 0 files | 0 files | PASS |

Non-zero exit matters: a caller that only checks the exit code must not read a
partial replay as a complete one.

---

## 4. What the test found

The value was not the 23 passes. It was this:

### A scoped delete was silently a global delete

The first cleanup pass called `delete_all(scope=GLOBAL, scope_key="global")`.
`scope_identifiers` emits **only `user_id`** for the GLOBAL scope, and the bulk
endpoint `DELETE /memories` filters on the identifier triple alone — so the call
became `DELETE /memories?user_id=lautaro` and removed **every memory that user
had, in every scope.**

Measured, destructively: the seeded evaluation set went from 5 memories to 0
while the caller had asked only for the shared scope. Nothing errored.

**Fix:** `delete_all` no longer uses the bulk endpoint. It enumerates the scope —
a listing that is already scope-filtered client-side — and deletes by id. Slower,
and cannot over-reach. It now returns the count it removed.

Guarded by two tests that fail against the old behaviour:
`test_delete_all_for_the_global_scope_does_not_delete_other_scopes` asserts a
repository memory is *not* deleted and that the bulk endpoint is *not* called.

Verified afterwards: the same test run now leaves
`repository:memory-optimization → 5 memories` and `global:global → 1 memory`
intact.

### The test itself was destroying real data

Even with `delete_all` fixed, cleaning up by *scope* wiped the real shared
memories, because the test writes into the same shared scope a human uses. The
harness now cleans up **by topic**, deleting only the five fixtures it created.

Worth stating plainly: a test that tidies up by scope is indistinguishable from
a test that destroys your data, right up until it does.

### PowerShell 5.1 reads `.ps1` as ANSI without a BOM

The first run of the harness did nothing at all: `Gestión` in the checkout path
decoded as `GestiÃ³n`, so every command failed with "file not found" — and the
results table dutifully reported 2 of 15 passing, which looked like a product
failure rather than a harness failure. Same root cause as an earlier `ctx.ps1`
bug. Both files now carry a BOM.

`(if ... {...} else {...})` is also not a valid expression in PS 5.1; it needs
`$(if ...)`.

---

## 5. Reproducing

```powershell
.\scripts\test-offline-spool.ps1
```

Requires the stack running and `server/.env` present. It isolates its own spool,
simulates the outage against a closed port, and cleans up by topic. It leaves
your real memories alone — verified, not assumed.

Unit coverage lives in `tests/context_memory/test_spool.py` (18 tests) and runs
with no server:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/context_memory -q
```

## 6. Limits

- **The spool is per-machine, not shared between machines.** Two people on two
  laptops have two queues. Nothing syncs them.
- **No automatic flush.** Replay is explicit: `ctx flush`, or the command the
  warning prints. A background flusher would replay without anyone seeing the
  report, which is the wrong default for something that writes.
- **A corrupt spool file is reported and left in place, forever**, until someone
  looks. That is deliberate — deleting what will not parse destroys the only
  copy of a memory whose file merely got truncated — but it does mean the
  pending count can stay non-zero until a human intervenes.
- **`is_unreachable` matches on exception type and message text.** A transport
  failure shaped differently from the ones listed would fail the write instead of
  queueing it. It fails toward *not* queueing, which is the safer direction: you
  see an error rather than silently accruing a queue.
