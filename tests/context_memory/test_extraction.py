import pytest

from src.memory.extraction import (
    Candidate,
    ExtractionReport,
    HeuristicExtractor,
    SessionArtifacts,
    dedupe_candidates,
    filter_storable,
)
from src.memory.scopes import Scope


def artifacts(**kw) -> SessionArtifacts:
    base = dict(
        summary="",
        commits=(),
        test_failures=(),
        tool_errors=(),
        decisions=(),
        repository="memory-optimization",
    )
    base.update(kw)
    return SessionArtifacts(**base)


# ---------------------------------------------------------------- heuristics


def test_a_conventional_commit_marked_breaking_becomes_a_decision():
    candidates = HeuristicExtractor().extract(
        artifacts(commits=("feat(api)!: drop the v1 memories endpoint",))
    )
    assert [c.kind for c in candidates] == ["decision"]
    assert "v1 memories endpoint" in candidates[0].text


def test_an_ordinary_feature_commit_is_not_a_memory():
    # Commit history is already in git. Copying it into Mem0 creates a second,
    # staler copy of something the repository layer retrieves live.
    assert HeuristicExtractor().extract(artifacts(commits=("feat(api): add a field",))) == []


def test_a_fix_commit_with_a_stated_cause_becomes_a_lesson():
    candidates = HeuristicExtractor().extract(
        artifacts(
            commits=(
                "fix(server): pin gemini model\n\nBecause gemini-2.0-flash now returns 404 NOT_FOUND.",
            )
        )
    )
    assert [c.kind for c in candidates] == ["lesson"]
    assert "404" in candidates[0].text


def test_the_cause_capture_stops_at_the_paragraph_it_started_in():
    # Regression: a DOTALL `.+` swallowed the rest of the commit body, so the
    # candidate text ran on into every later paragraph.
    commit = (
        "fix(server): pin the gemini model\n\n"
        "Because gemini-2.0-flash now returns 404 NOT_FOUND.\n\n"
        "Also here:\n\n"
        "- an unrelated note about the Makefile\n"
        "- another unrelated bullet\n"
    )
    candidates = HeuristicExtractor().extract(artifacts(commits=(commit,)))
    assert "404 NOT_FOUND" in candidates[0].text
    assert "Makefile" not in candidates[0].text
    assert "unrelated" not in candidates[0].text


def test_git_trailers_never_reach_a_candidate():
    # Co-Authored-By / session URLs are metadata about the commit, not knowledge.
    commit = (
        "fix(server): pin the gemini model\n\n"
        "Because gemini-2.0-flash now returns 404.\n\n"
        "Co-Authored-By: Someone <noreply@example.com>\n"
        "Claude-Session: https://example.com/session_abc\n"
        "Signed-off-by: Someone Else <x@example.com>\n"
    )
    candidates = HeuristicExtractor().extract(artifacts(commits=(commit,)))
    text = candidates[0].text
    assert "Co-Authored-By" not in text
    assert "Claude-Session" not in text
    assert "Signed-off-by" not in text
    assert "example.com" not in text


def test_a_tool_error_becomes_a_discovery_with_the_error_text_kept_verbatim():
    candidates = HeuristicExtractor().extract(
        artifacts(tool_errors=("pip install failed: CERTIFICATE_VERIFY_FAILED unable to get local issuer",))
    )
    assert candidates[0].kind == "discovery"
    assert "CERTIFICATE_VERIFY_FAILED" in candidates[0].text


def test_a_recorded_decision_is_taken_as_written():
    candidates = HeuristicExtractor().extract(
        artifacts(decisions=("Use gemini for both legs because anthropic ships no embedder.",))
    )
    assert candidates[0].kind == "decision"
    assert candidates[0].text.startswith("Use gemini")


def test_a_test_failure_that_was_fixed_becomes_a_failure_memory():
    candidates = HeuristicExtractor().extract(
        artifacts(test_failures=("test_search_filters: expected 2 results, got 0 - filters were not sent",))
    )
    assert candidates[0].kind == "failure"


def test_candidates_default_to_repository_scope_and_candidate_lifecycle():
    candidates = HeuristicExtractor().extract(artifacts(decisions=("A decision.",)))
    assert candidates[0].scope is Scope.REPOSITORY
    assert candidates[0].scope_key == "memory-optimization"
    assert candidates[0].lifecycle == "candidate"


def test_nothing_is_extracted_from_empty_artifacts():
    assert HeuristicExtractor().extract(artifacts()) == []


def test_every_candidate_carries_the_artifact_it_came_from():
    # A candidate a reviewer cannot trace back is a candidate they cannot judge.
    candidates = HeuristicExtractor().extract(artifacts(decisions=("A decision.",)))
    assert candidates[0].evidence == "A decision."


def test_a_candidate_gets_a_topic_when_the_text_names_one_and_none_otherwise():
    with_topic = HeuristicExtractor().extract(
        artifacts(tool_errors=("server/main.py: provider gemini rejected",))
    )
    assert with_topic[0].topic is not None
    without = HeuristicExtractor().extract(artifacts(decisions=("Something general.",)))
    assert without[0].topic is None


# ------------------------------------------------------------------- filters


def test_storable_rejects_a_candidate_that_only_restates_the_repository():
    repo_text = "The server exposes POST /memories and POST /search."
    kept, report = filter_storable(
        [Candidate(text=repo_text, kind="discovery", scope=Scope.REPOSITORY, scope_key="r", evidence="x")],
        repository_text=repo_text,
    )
    assert kept == []
    assert report.rejected["restates_repository"] == 1


def test_storable_rejects_text_shorter_than_a_usable_fact():
    kept, report = filter_storable(
        [Candidate(text="it broke", kind="lesson", scope=Scope.REPOSITORY, scope_key="r", evidence="x")],
        repository_text="",
    )
    assert kept == []
    assert report.rejected["too_short"] == 1


def test_storable_keeps_a_genuine_discovery():
    kept, _ = filter_storable(
        [
            Candidate(
                text="pgvector defaults embedding_model_dims to 1536 while the gemini embedder emits 768.",
                kind="discovery",
                scope=Scope.REPOSITORY,
                scope_key="r",
                evidence="x",
            )
        ],
        repository_text="unrelated repository content",
    )
    assert len(kept) == 1


def test_the_report_states_the_denominator():
    kept, report = filter_storable(
        [
            Candidate(text="short", kind="note", scope=Scope.GLOBAL, scope_key="g", evidence="x"),
            Candidate(
                text="A properly detailed discovery about provider behaviour worth keeping.",
                kind="discovery",
                scope=Scope.GLOBAL,
                scope_key="g",
                evidence="x",
            ),
        ],
        repository_text="",
    )
    assert report.summary() == "1 of 2 candidates storable, 1 rejected (too_short 1)"


def test_a_repository_scoped_candidate_is_never_silently_promoted_to_global():
    # Repository facts at global scope surface on unrelated work as universal truth.
    kept, _ = filter_storable(
        [
            Candidate(
                text="This repository stores memories under scope keys rather than raw user ids.",
                kind="convention",
                scope=Scope.REPOSITORY,
                scope_key="memory-optimization",
                evidence="x",
            )
        ],
        repository_text="",
    )
    assert kept[0].scope is Scope.REPOSITORY


# ------------------------------------------------------------------- dedupe


def test_two_candidates_saying_the_same_thing_collapse():
    kept = dedupe_candidates(
        [
            Candidate(text="The embedder emits 768 dimensions here.", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="a"),
            Candidate(text="The embedder emits 768 dimensions here", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="b"),
        ]
    )
    assert len(kept) == 1


def test_dedupe_keeps_the_evidence_of_both():
    kept = dedupe_candidates(
        [
            Candidate(text="The embedder emits 768 dimensions here.", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="a"),
            Candidate(text="The embedder emits 768 dimensions here", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="b"),
        ]
    )
    assert "a" in kept[0].evidence and "b" in kept[0].evidence


def test_distinct_candidates_both_survive():
    kept = dedupe_candidates(
        [
            Candidate(text="The embedder emits 768 dimensions.", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="a"),
            Candidate(text="The dashboard build needs a CA certificate.", kind="discovery", scope=Scope.GLOBAL, scope_key="g", evidence="b"),
        ]
    )
    assert len(kept) == 2


# -------------------------------------------------------------- storage gate


def test_a_candidate_cannot_be_constructed_as_durable():
    # Promotion is a separate deliberate act. Extraction never produces durable
    # memory, or the review gate has been bypassed by construction.
    with pytest.raises(ValueError, match="candidate"):
        Candidate(
            text="A fact that is long enough to pass.",
            kind="discovery",
            scope=Scope.GLOBAL,
            scope_key="g",
            evidence="x",
            lifecycle="durable",
        )


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown kind"):
        Candidate(
            text="A fact that is long enough to pass.",
            kind="epiphany",
            scope=Scope.GLOBAL,
            scope_key="g",
            evidence="x",
        )


def test_extraction_report_names_zero_explicitly_rather_than_printing_nothing():
    report = ExtractionReport(considered=0)
    assert "0 of 0" in report.summary()
