"""Build a context for a task and print it, or print only its report.

    python -m src.scripts.context_build --task "add graph memory to the stack"
    python -m src.scripts.context_build --task "..." --files server/main.py --report-only

Runs without a Mem0 server: the memory layer is simply absent and the report
says so. That is deliberate - the three static layers are useful on their own,
and a missing memory service must degrade visibly rather than silently.
"""

from __future__ import annotations

import argparse
import os
import sys

from src.context.manager import ContextManager, ContextRequest
from src.context.sources.memory import MemoryContextSource
from src.context.types import Task
from src.memory.scopes import ScopeSelector
from src.scripts._common import add_common_arguments, configure_stdout, emit, repo_root


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="what the agent is about to do")
    parser.add_argument("--files", nargs="*", default=[], help="paths the task touches")
    parser.add_argument("--repository", default=os.environ.get("MEM0_REPOSITORY"))
    parser.add_argument("--branch", default=None)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--root", default=None, help="repository root (defaults to this repo)")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--no-memory", action="store_true", help="skip the Mem0 layer entirely")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    root = args.root or repo_root()

    memory_source = None
    if not args.no_memory and os.environ.get("MEM0_API_URL"):
        from src.memory.mem0_provider import Mem0Provider

        try:
            provider = Mem0Provider.from_env()
        except RuntimeError as error:
            print(f"warning: memory layer unavailable: {error}", file=sys.stderr)
            provider = None
        if provider is not None:
            memory_source = MemoryContextSource(
                provider=provider,
                selector=ScopeSelector(
                    user=os.environ.get("MEM0_USER", "unknown"),
                    repository=args.repository,
                    project=os.environ.get("MEM0_PROJECT"),
                    branch=args.branch,
                ),
                top_k=args.top_k,
            )

    manager = ContextManager.from_repo(root=root, memory_source=memory_source, max_tokens=args.max_tokens)
    result = manager.build(
        ContextRequest(
            task=Task(
                description=args.task,
                files=tuple(args.files),
                repository=args.repository,
                branch=args.branch,
            ),
            max_tokens=args.max_tokens,
        )
    )

    if args.json:
        emit(
            {
                "text": None if args.report_only else result.text,
                "report": {
                    "candidates": result.report.candidates,
                    "injected": result.report.injected,
                    "dropped_duplicate": result.report.dropped_duplicate,
                    "dropped_budget": result.report.dropped_budget,
                    "duplicate_rate": result.report.duplicate_rate,
                    "degraded": result.report.degraded,
                    "source_counts": result.report.source_counts,
                    "source_errors": result.report.source_errors,
                    "stages_ms": result.report.stages,
                    "tokens": result.report.budget_report.total_tokens if result.report.budget_report else 0,
                },
            },
            True,
            "",
        )
        return 0

    if args.report_only:
        print(result.report.summary())
        print("\nper source:")
        for name, count in result.report.source_counts.items():
            print(f"  {name}: {count} candidates")
        print("\nstage timings (ms):")
        for name, ms in result.report.stages.items():
            print(f"  {name}: {ms:.1f}")
        return 0

    print(result.text)
    print("\n---\n" + result.report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
