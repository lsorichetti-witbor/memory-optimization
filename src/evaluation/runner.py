"""Run the five configurations from handoff section 18 phase 6 over a dataset.

    full                    every candidate, no selection (the information ceiling)
    static-docs-only        instructions + repository
    memory-only             Mem0 only
    static+memory           instructions + repository + memory
    static+memory+state     all four layers - the full Context Manager

`full` is the control. It is the most informed arm and the least efficient one.
If `static+memory+state` does not beat it on tokens at comparable recall, the
Context Manager is not earning its place, and the harness should say so rather
than reporting a number that looks good in isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from src.context.budget import LayerFloors
from src.context.manager import ContextManager, ContextRequest
from src.context.sources.base import ContextSource
from src.context.sources.current_state import CurrentStateSource
from src.context.sources.instructions import InstructionsSource
from src.context.sources.memory import MemoryContextSource
from src.context.sources.repository import RepositorySource
from src.context.types import Layer, Task
from src.evaluation.dataset import EvalCase, EvalDataset
from src.evaluation.metrics import mean_defined, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank
from src.evaluation.report import ArmResult, RunReport

# A budget large enough that the `full` arm is not silently truncated: the point
# of that arm is to be the information ceiling.
FULL_ARM_BUDGET = 1_000_000


@dataclass(frozen=True)
class Arm:
    name: str
    layers: frozenset[Layer]
    budget: Optional[int] = None


ARMS: tuple[Arm, ...] = (
    Arm("full", frozenset(Layer), budget=FULL_ARM_BUDGET),
    Arm("static-docs-only", frozenset({Layer.INSTRUCTIONS, Layer.REPOSITORY})),
    Arm("memory-only", frozenset({Layer.MEMORY})),
    Arm("static+memory", frozenset({Layer.INSTRUCTIONS, Layer.REPOSITORY, Layer.MEMORY})),
    Arm("static+memory+state", frozenset(Layer)),
)


def build_sources(
    root: Path,
    memory_source_factory: Optional[Callable[[], MemoryContextSource]] = None,
) -> dict[Layer, ContextSource]:
    sources: dict[Layer, ContextSource] = {
        Layer.INSTRUCTIONS: InstructionsSource(root=root),
        Layer.REPOSITORY: RepositorySource(root=root),
        Layer.CURRENT_STATE: CurrentStateSource(root=root),
    }
    if memory_source_factory is not None:
        sources[Layer.MEMORY] = memory_source_factory()
    return sources


def _item_key(item) -> str:
    """What counts as "the item" for ground truth.

    Chunk ids are content-derived and change whenever a heading is edited, so a
    dataset keyed on them would rot on the next docs change. Source paths are
    stable.

    Memory items key on their `topic`, not their id. A Mem0 id is a fresh uuid
    on every re-seed, so a dataset keyed on ids would score zero for the memory
    layer forever - and it would look exactly like poor retrieval rather than
    like a broken key. A memory with no topic falls back to its id and is
    effectively unscoreable, which is one more reason to always set a topic.
    """
    if item.layer is not Layer.MEMORY:
        return item.source
    topic = item.metadata.get("topic")
    return f"mem0:{topic}" if topic else f"mem0:id:{item.id}"


def run_case(
    manager: ContextManager,
    case: EvalCase,
    top_k: int,
    budget: int,
) -> tuple[list[str], int, int, float, int, float]:
    task = Task(
        description=case.question,
        files=case.files,
        repository=case.repository,
    )
    started = time.perf_counter()
    result = manager.build(ContextRequest(task=task, max_tokens=budget))
    elapsed = (time.perf_counter() - started) * 1000

    retrieved: list[str] = []
    for item in result.items:
        key = _item_key(item)
        if key not in retrieved:
            retrieved.append(key)

    tokens = result.report.budget_report.total_tokens if result.report.budget_report else 0
    memories = sum(1 for i in result.items if i.layer is Layer.MEMORY)
    return retrieved, tokens, memories, elapsed, result.report.candidates, result.report.duplicate_rate


def run_arm(
    arm: Arm,
    dataset: EvalDataset,
    root: Path,
    top_k: int,
    budget: int,
    memory_source_factory: Optional[Callable[[], MemoryContextSource]] = None,
) -> ArmResult:
    recalls: list[Optional[float]] = []
    precisions: list[Optional[float]] = []
    rrs: list[float] = []
    ndcgs: list[float] = []
    tokens_total = 0
    memories_total = 0
    latency_total = 0.0
    candidates_total = 0
    duplicate_rates: list[float] = []

    arm_budget = arm.budget or budget

    for case in dataset.cases:
        # A fresh manager per case: sources cache their last report, and reusing
        # one would blend cases together in the reports.
        available = build_sources(root, memory_source_factory)
        sources = [s for layer, s in available.items() if layer in arm.layers]
        manager = ContextManager(sources=sources, max_tokens=arm_budget, floors=LayerFloors())

        retrieved, tokens, memories, elapsed, candidates, dup = run_case(manager, case, top_k, arm_budget)

        relevant = set(case.relevant)
        recalls.append(recall_at_k(retrieved, relevant, top_k))
        precisions.append(precision_at_k(retrieved, relevant, top_k))
        rrs.append(reciprocal_rank(retrieved, relevant))
        ndcgs.append(ndcg_at_k(retrieved, relevant, top_k))
        tokens_total += tokens
        memories_total += memories
        latency_total += elapsed
        candidates_total += candidates
        duplicate_rates.append(dup)

    n = max(1, dataset.size)
    recall_mean, scoreable, total = mean_defined(recalls)
    precision_mean, _, _ = mean_defined(precisions)

    return ArmResult(
        name=arm.name,
        recall_at_k=recall_mean,
        precision_at_k=precision_mean,
        mrr=sum(rrs) / n,
        ndcg=sum(ndcgs) / n,
        context_tokens=round(tokens_total / n),
        memories_injected=round(memories_total / n),
        latency_ms=latency_total / n,
        candidates=round(candidates_total / n),
        scoreable_cases=scoreable,
        total_cases=total,
        duplicate_rate=sum(duplicate_rates) / n if duplicate_rates else 0.0,
    )


def unreachable_ground_truth(
    dataset: EvalDataset,
    root: Path,
    memory_source_factory: Optional[Callable[[], MemoryContextSource]] = None,
) -> set[str]:
    """Ground-truth items no source produces, at any budget.

    Without this the report cannot distinguish "ranked badly" from "never a
    candidate", and a coverage gap reads as a scoring problem - which is the
    wrong thing to go and fix.
    """
    missing: set[str] = set()
    for case in dataset.cases:
        sources = list(build_sources(root, memory_source_factory).values())
        manager = ContextManager(sources=sources, max_tokens=FULL_ARM_BUDGET)
        result = manager.build(
            ContextRequest(
                task=Task(description=case.question, files=case.files, repository=case.repository),
                max_tokens=FULL_ARM_BUDGET,
            )
        )
        produced = {_item_key(i) for i in result.items}
        missing |= set(case.relevant) - produced
    return missing


def run_all(
    dataset: EvalDataset,
    root: Path,
    top_k: int = 10,
    budget: int = 8000,
    memory_source_factory: Optional[Callable[[], MemoryContextSource]] = None,
    arms: Sequence[Arm] = ARMS,
) -> RunReport:
    report = RunReport(
        dataset=dataset.name,
        dataset_size=dataset.size,
        model="none (retrieval only)",
        top_k=top_k,
        reranker="none",
        token_budget=budget,
        runs=1,
        self_reported=True,
        notes=(
            dataset.provenance(),
            "Memory layer present only when a Mem0 server was reachable; arms "
            "naming memory score n/a without one, rather than zero.",
            "The `full` arm runs with an effectively unlimited budget so it is the "
            "information ceiling, not a differently-budgeted competitor.",
        ),
    )
    unreachable = unreachable_ground_truth(dataset, root, memory_source_factory)
    if unreachable:
        report.notes = report.notes + (
            f"CEILING: {len(unreachable)} ground-truth item(s) are not collected by any "
            f"source, so no arm can reach them and recall is capped below 1.0 by "
            f"construction: {', '.join(sorted(unreachable))}. This is a source coverage "
            f"limit, not a ranking failure - read it before treating a low recall as a "
            f"scoring problem.",
        )

    for arm in arms:
        if Layer.MEMORY in arm.layers and memory_source_factory is None and arm.layers == {Layer.MEMORY}:
            # memory-only with no memory service cannot be scored at all. Record
            # it as unscoreable rather than as a zero that looks like a result.
            report.add(
                ArmResult(
                    name=arm.name, recall_at_k=None, precision_at_k=None, mrr=0.0, ndcg=0.0,
                    context_tokens=0, memories_injected=0, latency_ms=0.0,
                    scoreable_cases=0, total_cases=dataset.size,
                )
            )
            continue
        report.add(run_arm(arm, dataset, root, top_k, budget, memory_source_factory))
    return report
