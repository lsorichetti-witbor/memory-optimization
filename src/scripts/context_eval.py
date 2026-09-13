"""Run the evaluation harness and print a methodology-complete report.

    python -m src.scripts.context_eval
    python -m src.scripts.context_eval --dataset src/evaluation/datasets/repo-local.json --top-k 10
    python -m src.scripts.context_eval --out docs/superpowers/evaluations/run.md

Retrieval-only. It measures what the Context Manager selects, not whether an
agent given that context answers correctly, and the report says so on every run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.context.sources.memory import MemoryContextSource
from src.evaluation.dataset import EvalDataset
from src.evaluation.runner import run_all
from src.memory.scopes import ScopeSelector
from src.scripts._common import configure_stdout, repo_root

DEFAULT_DATASET = "src/evaluation/datasets/repo-local.json"


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=None, help=f"path to a dataset json (default: {DEFAULT_DATASET})")
    parser.add_argument("--root", default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--budget", type=int, default=8000)
    parser.add_argument("--out", default=None, help="write the report here instead of stdout")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="write the memories the dataset's mem0:<topic> ground truth refers to, then run",
    )
    args = parser.parse_args(argv)

    root = Path(args.root or repo_root())
    dataset_path = Path(args.dataset) if args.dataset else root / DEFAULT_DATASET
    if not dataset_path.is_file():
        print(f"error: dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    dataset = EvalDataset.from_json(dataset_path)

    memory_factory = None
    if not args.no_memory and os.environ.get("MEM0_API_URL"):
        from src.memory.mem0_provider import Mem0Provider

        if args.seed:
            from src.evaluation.seed import seed as seed_memories
            from src.memory.scopes import Scope

            scope_key = os.environ.get("MEM0_REPOSITORY")
            if not scope_key:
                print("error: --seed needs MEM0_REPOSITORY to know which scope to write", file=sys.stderr)
                return 2
            seeding_provider = Mem0Provider.from_env()
            try:
                written = seed_memories(seeding_provider, scope_key=scope_key, scope=Scope.REPOSITORY)
            except RuntimeError as error:
                print(f"error: seeding failed: {error}", file=sys.stderr)
                return 1
            finally:
                seeding_provider.close()
            print(f"seeded {len(written)} memories into repository:{scope_key}", file=sys.stderr)

        def memory_factory():  # noqa: F811 - deliberate closure over env
            return MemoryContextSource(
                provider=Mem0Provider.from_env(),
                selector=ScopeSelector(
                    user=os.environ.get("MEM0_USER", "unknown"),
                    repository=os.environ.get("MEM0_REPOSITORY"),
                    project=os.environ.get("MEM0_PROJECT"),
                ),
                top_k=args.top_k,
            )

    report = run_all(
        dataset=dataset,
        root=root,
        top_k=args.top_k,
        budget=args.budget,
        memory_source_factory=memory_factory,
    )
    if memory_factory is None:
        report.notes = report.notes + (
            "No Mem0 server was reachable (MEM0_API_URL unset or --no-memory). "
            "Arms naming the memory layer measured the static layers only.",
        )

    text = report.render()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
