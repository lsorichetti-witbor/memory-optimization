from datetime import datetime, timedelta, timezone

from src.context.sources.memory import MemoryContextSource
from src.context.types import Layer, Task
from src.memory.base import MemoryProvider
from src.memory.envelope import Envelope
from src.memory.scopes import Scope, ScopeSelector
from src.memory.types import MemoryRecord


class FakeProvider(MemoryProvider):
    def __init__(self, by_scope):
        self.by_scope = by_scope
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.by_scope.get(query.scope, [])

    def add(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def get(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def get_all(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def update(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def delete(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def delete_all(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def history(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def reset(self):  # pragma: no cover
        raise NotImplementedError


def record(
    id_,
    text,
    scope,
    *,
    age_days=1,
    confidence=0.8,
    importance=0.5,
    lifecycle="durable",
    score=0.5,
    topic=None,
):
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return MemoryRecord(
        id=id_,
        text=text,
        score=score,
        created_at=created,
        envelope=Envelope(
            scope=scope,
            scope_key="k",
            kind="discovery",
            lifecycle=lifecycle,
            confidence=confidence,
            importance=importance,
            topic=topic,
            source="s",
            created_at=created,
        ),
    )


def source(by_scope, **kwargs):
    selector = kwargs.pop("selector", ScopeSelector(user="u"))
    return MemoryContextSource(provider=FakeProvider(by_scope), selector=selector, **kwargs)


def test_queries_every_scope_that_has_a_key():
    selector = ScopeSelector(user="lautaro", repository="memory-optimization")
    s = source({}, selector=selector)
    s.collect(Task(description="x"))
    assert [q.scope for q in s._provider.queries] == [Scope.REPOSITORY, Scope.USER, Scope.GLOBAL]


def test_maps_records_to_memory_layer_items_carrying_provider_score_as_relevance():
    items = source({Scope.GLOBAL: [record("m1", "a lesson", Scope.GLOBAL, score=0.66)]}).collect(
        Task(description="x")
    )
    assert [i.layer for i in items] == [Layer.MEMORY]
    assert items[0].signals.relevance == 0.66


def test_a_record_with_no_provider_score_is_not_treated_as_perfectly_relevant():
    items = source({Scope.GLOBAL: [record("m1", "a lesson", Scope.GLOBAL, score=None)]}).collect(
        Task(description="x")
    )
    assert items[0].signals.relevance == 0.0


def test_recency_decays_with_age():
    items = {
        i.id: i
        for i in source(
            {
                Scope.GLOBAL: [
                    record("new", "x", Scope.GLOBAL, age_days=1),
                    record("old", "y", Scope.GLOBAL, age_days=400),
                ]
            }
        ).collect(Task(description="x"))
    }
    assert items["new"].signals.recency > items["old"].signals.recency


def test_stale_memories_are_returned_flagged_not_dropped():
    # Dropping them here would hide a contradiction the compiler needs to report.
    items = source({Scope.GLOBAL: [record("m1", "old truth", Scope.GLOBAL, lifecycle="stale")]}).collect(
        Task(description="x")
    )
    assert items[0].signals.staleness == 1.0


def test_superseded_memories_are_never_returned():
    items = source({Scope.GLOBAL: [record("m1", "replaced", Scope.GLOBAL, lifecycle="superseded")]}).collect(
        Task(description="x")
    )
    assert items == []


def test_the_same_memory_returned_from_two_scopes_appears_once():
    r = record("m1", "a lesson", Scope.GLOBAL)
    items = source({Scope.GLOBAL: [r], Scope.USER: [r]}, selector=ScopeSelector(user="u")).collect(
        Task(description="x")
    )
    assert [i.id for i in items] == ["m1"]


def test_metadata_carries_the_scope_so_the_ranker_can_match_it():
    items = source({Scope.GLOBAL: [record("m1", "x", Scope.REPOSITORY, topic="vector_store")]}).collect(
        Task(description="x")
    )
    assert items[0].metadata["scope"] == "repo"
    assert items[0].metadata["topic"] == "vector_store"


def test_age_in_days_is_exposed_for_rendering():
    items = source({Scope.GLOBAL: [record("m1", "x", Scope.GLOBAL, age_days=120)]}).collect(Task(description="x"))
    assert items[0].metadata["age_days"] == 120


def test_a_provider_failure_is_surfaced_not_swallowed():
    class Broken(FakeProvider):
        def search(self, query):
            raise RuntimeError("upstream 503")

    s = MemoryContextSource(provider=Broken({}), selector=ScopeSelector(user="u"))
    items = s.collect(Task(description="x"))
    assert items == []
    assert "upstream 503" in str(s.last_error)


def test_confidence_and_importance_come_from_the_envelope():
    items = source(
        {Scope.GLOBAL: [record("m1", "x", Scope.GLOBAL, confidence=0.3, importance=0.9)]}
    ).collect(Task(description="x"))
    assert items[0].signals.confidence == 0.3
    assert items[0].signals.importance == 0.9
