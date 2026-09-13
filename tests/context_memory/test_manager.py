from src.context.manager import ContextManager, ContextRequest
from src.context.sources.base import ContextSource
from src.context.types import ContextItem, Layer, Task


class StubSource(ContextSource):
    def __init__(self, name, layer, items, error=None):
        self._name = name
        self._layer = layer
        self._items = items
        self.last_error = error

    @property
    def name(self):
        return self._name

    @property
    def layer(self):
        return self._layer

    def collect(self, task):
        if self.last_error:
            return []
        return [ContextItem(**{**i.__dict__}) for i in self._items]


def item(id_, layer, content, pinned=False):
    return ContextItem(id=id_, layer=layer, content=content, source=f"{id_}.md", pinned=pinned)


def manager(sources, max_tokens=10_000):
    return ContextManager(sources=sources, max_tokens=max_tokens)


def test_builds_a_context_from_every_source():
    result = manager(
        [
            StubSource(
                "i", Layer.INSTRUCTIONS, [item("i1", Layer.INSTRUCTIONS, "Always verify numbers.", pinned=True)]
            ),
            StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "pgvector is the store.")]),
            StubSource("s", Layer.CURRENT_STATE, [item("s1", Layer.CURRENT_STATE, "branch: main")]),
            StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "gemini covers both llm and embedder.")]),
        ]
    ).build(ContextRequest(task=Task(description="which provider do we use")))
    assert "Always verify numbers." in result.text
    assert "gemini covers both" in result.text


def test_the_report_accounts_for_every_candidate():
    result = manager(
        [StubSource("m", Layer.MEMORY, [item(f"m{i}", Layer.MEMORY, f"fact number {i}") for i in range(5)])]
    ).build(ContextRequest(task=Task(description="fact")))
    r = result.report
    assert r.candidates == 5
    assert r.injected + r.dropped_duplicate + r.dropped_budget == r.candidates


def test_a_failing_source_is_reported_not_hidden():
    result = manager(
        [
            StubSource("m", Layer.MEMORY, [], error=RuntimeError("upstream 503")),
            StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "x")]),
        ]
    ).build(ContextRequest(task=Task(description="x")))
    assert "upstream 503" in result.report.source_errors["m"]
    assert result.report.degraded is True


def test_a_context_built_with_no_memory_source_is_not_marked_degraded():
    result = manager([StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "x")])]).build(
        ContextRequest(task=Task(description="x"))
    )
    assert result.report.degraded is False


def test_instructions_survive_a_budget_that_cannot_fit_anything_else():
    result = manager(
        [
            StubSource("i", Layer.INSTRUCTIONS, [item("i1", Layer.INSTRUCTIONS, "Rule.", pinned=True)]),
            StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "x" * 4000)]),
        ],
        max_tokens=30,
    ).build(ContextRequest(task=Task(description="x")))
    assert "Rule." in result.text
    assert result.report.dropped_budget == 1


def test_the_pipeline_stages_are_all_reported():
    result = manager([StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "x")])]).build(
        ContextRequest(task=Task(description="x"))
    )
    assert set(result.report.stages) == {"collect", "rank", "dedupe", "conflicts", "budget", "compile"}
    assert all(v >= 0 for v in result.report.stages.values())


def test_build_is_deterministic():
    def sources():
        return [
            StubSource(
                "r",
                Layer.REPOSITORY,
                [item("r1", Layer.REPOSITORY, "alpha content"), item("r2", Layer.REPOSITORY, "beta content")],
            )
        ]

    first = manager(sources()).build(ContextRequest(task=Task(description="alpha beta")))
    second = manager(sources()).build(ContextRequest(task=Task(description="alpha beta")))
    assert first.text == second.text


def test_the_conflict_and_dedupe_reports_are_carried_through():
    result = manager([StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "a fact")])]).build(
        ContextRequest(task=Task(description="x"))
    )
    assert result.report.conflict_report is not None
    assert result.report.budget_report is not None
    assert result.report.duplicate_rate == 0.0


def test_a_per_request_budget_overrides_the_managers_default():
    small = manager(
        [StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "x" * 4000)])], max_tokens=10_000
    ).build(ContextRequest(task=Task(description="x"), max_tokens=10))
    assert small.report.dropped_budget == 1


def test_the_report_renders_a_summary_with_denominators():
    result = manager(
        [StubSource("m", Layer.MEMORY, [item(f"m{i}", Layer.MEMORY, f"fact number {i}") for i in range(4)])]
    ).build(ContextRequest(task=Task(description="fact")))
    summary = result.report.summary()
    assert "of 4" in summary
