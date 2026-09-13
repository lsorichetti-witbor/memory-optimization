import pytest

from src.evaluation.dataset import EvalCase, EvalDataset
from src.evaluation.metrics import ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank
from src.evaluation.report import ArmResult, RunReport


# ------------------------------------------------------------------- metrics


def test_recall_counts_relevant_items_found():
    assert recall_at_k(retrieved=["a", "b", "c"], relevant={"a", "c"}, k=3) == 1.0
    assert recall_at_k(retrieved=["a", "x", "y"], relevant={"a", "c"}, k=3) == 0.5


def test_recall_respects_k():
    assert recall_at_k(retrieved=["x", "y", "a"], relevant={"a"}, k=2) == 0.0
    assert recall_at_k(retrieved=["x", "y", "a"], relevant={"a"}, k=3) == 1.0


def test_recall_with_no_relevant_items_is_undefined_not_zero():
    # Returning 0.0 would drag a mean down with a case that cannot be scored.
    assert recall_at_k(retrieved=["a"], relevant=set(), k=1) is None


def test_precision_counts_how_much_of_what_was_shown_was_relevant():
    assert precision_at_k(retrieved=["a", "x"], relevant={"a"}, k=2) == 0.5


def test_precision_divides_by_what_was_actually_returned_not_by_k():
    # Dividing by k punishes a system for a short but perfect result list.
    assert precision_at_k(retrieved=["a"], relevant={"a"}, k=10) == 1.0


def test_precision_of_an_empty_result_is_undefined_not_zero():
    assert precision_at_k(retrieved=[], relevant={"a"}, k=5) is None


def test_reciprocal_rank_uses_the_first_hit():
    assert reciprocal_rank(retrieved=["x", "a", "b"], relevant={"a", "b"}) == 0.5
    assert reciprocal_rank(retrieved=["a"], relevant={"a"}) == 1.0


def test_reciprocal_rank_is_zero_when_nothing_relevant_was_retrieved():
    assert reciprocal_rank(retrieved=["x", "y"], relevant={"a"}) == 0.0


def test_ndcg_is_one_for_a_perfect_ranking():
    assert ndcg_at_k(retrieved=["a", "b"], relevant={"a", "b"}, k=2) == pytest.approx(1.0)


def test_ndcg_punishes_a_relevant_item_ranked_lower():
    good = ndcg_at_k(retrieved=["a", "x"], relevant={"a"}, k=2)
    bad = ndcg_at_k(retrieved=["x", "a"], relevant={"a"}, k=2)
    assert good > bad


def test_ndcg_is_bounded():
    value = ndcg_at_k(retrieved=["x", "a", "b"], relevant={"a", "b"}, k=3)
    assert 0.0 <= value <= 1.0


# ------------------------------------------------------------------- dataset


def test_a_case_must_name_at_least_one_relevant_item():
    # A case with no ground truth silently scores every arm identically.
    with pytest.raises(ValueError, match="relevant"):
        EvalCase(id="c1", question="why?", relevant=frozenset(), answer_contains=())


def test_dataset_reports_its_size_and_provenance():
    ds = EvalDataset(
        name="repo-local",
        description="questions about this repository",
        cases=(EvalCase(id="c1", question="q", relevant=frozenset({"a"}), answer_contains=("x",)),),
    )
    assert ds.size == 1
    assert "repo-local" in ds.provenance()
    assert "1 case" in ds.provenance()


def test_dataset_rejects_duplicate_case_ids():
    case = EvalCase(id="c1", question="q", relevant=frozenset({"a"}), answer_contains=())
    with pytest.raises(ValueError, match="duplicate"):
        EvalDataset(name="x", description="d", cases=(case, case))


# -------------------------------------------------------------------- report


def test_report_records_the_methodology_block():
    report = RunReport(
        dataset="repo-local",
        dataset_size=4,
        model="none (retrieval only)",
        top_k=10,
        reranker="none",
        token_budget=8000,
        runs=1,
        self_reported=True,
    )
    text = report.render()
    for field in ("dataset", "top_k", "reranker", "token_budget", "runs", "self_reported"):
        assert field in text


def test_report_refuses_to_present_retrieval_recall_as_answer_accuracy():
    report = RunReport(
        dataset="repo-local", dataset_size=1, model="none (retrieval only)",
        top_k=5, reranker="none", token_budget=100, runs=1, self_reported=True,
    )
    report.add(ArmResult(name="static+memory", recall_at_k=0.9, precision_at_k=0.5, mrr=0.8, ndcg=0.85,
                         context_tokens=100, memories_injected=2, latency_ms=12.0))
    text = report.render()
    assert "retrieval only" in text.lower()
    assert "answer_accuracy" not in text


def test_report_prints_every_arm_with_its_denominator():
    report = RunReport(
        dataset="repo-local", dataset_size=4, model="none (retrieval only)",
        top_k=5, reranker="none", token_budget=100, runs=1, self_reported=True,
    )
    for name in ("full", "static-only"):
        report.add(ArmResult(name=name, recall_at_k=0.5, precision_at_k=0.5, mrr=0.5, ndcg=0.5,
                             context_tokens=10, memories_injected=0, latency_ms=1.0))
    text = report.render()
    assert "full" in text and "static-only" in text
    assert "4 cases" in text


def test_an_arm_with_no_scoreable_cases_is_reported_as_such_not_as_zero():
    result = ArmResult(name="memory-only", recall_at_k=None, precision_at_k=None, mrr=0.0,
                       ndcg=0.0, context_tokens=0, memories_injected=0, latency_ms=0.0)
    assert "n/a" in result.render_row()


def test_an_arm_whose_source_failed_raises_instead_of_scoring_zero(tmp_path):
    # A source that errored produced no items, and scoring that 0.0 reports an
    # outage as a measurement. Measured: a Gemini quota exhaustion made every
    # memory arm read 0.000 recall, indistinguishable from a ranking regression
    # until someone read the container log.
    import pytest as _pytest

    from src.context.sources.base import ContextSource
    from src.context.types import ContextItem, Layer, Task
    from src.evaluation.dataset import EvalCase, EvalDataset
    from src.evaluation.runner import Arm, SourceUnavailable, run_arm

    class BrokenSource(ContextSource):
        def __init__(self):
            self.last_error = RuntimeError("upstream 502: quota exhausted")

        @property
        def name(self):
            return "memory"

        @property
        def layer(self):
            return Layer.MEMORY

        def collect(self, task):
            return []

    (tmp_path / "README.md").write_text("# P\n\nx\n", encoding="utf-8")
    dataset = EvalDataset(
        name="t", description="d",
        cases=(EvalCase(id="c1", question="q", relevant=frozenset({"README.md"})),),
    )
    arm = Arm("memory-only", frozenset({Layer.MEMORY}))

    with _pytest.raises(SourceUnavailable, match="quota exhausted"):
        run_arm(arm, dataset, tmp_path, top_k=5, budget=1000, memory_source_factory=lambda: BrokenSource())
