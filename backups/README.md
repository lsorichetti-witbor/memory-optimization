# Memory backups

`python -m src.scripts.memory_backup export --out backups/memories-<stamp>.json`
writes here, and `restore --in <file>` reads it back.

The `.json` dumps are **gitignored on purpose**. They are a point-in-time export
of one machine's memory store and can contain anything the user chose to
remember. The versioned, reproducible copy of the memories that are *meant* to
exist everywhere is code:

- `src/evaluation/global_seed.py` — the global scope: rule incidents and
  retrieval findings
- `src/evaluation/seed.py` — the repository fixtures the benchmark scores against

Written back with `python -m src.scripts.memory_seed --global --repo <key>`.

A dump is what you take before doing something destructive. The seeders are what
makes a wipe recoverable without one.
