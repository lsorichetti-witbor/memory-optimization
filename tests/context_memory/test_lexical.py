from src.context.lexical import Bm25, tokenize


def test_tokenize_lowercases_and_keeps_identifiers_intact():
    assert tokenize("Set MEM0_DEFAULT_LLM_PROVIDER in server/main.py") == [
        "set",
        "mem0_default_llm_provider",
        "in",
        "server",
        "main",
        "py",
    ]


def test_bm25_ranks_the_document_containing_the_rare_term_first():
    docs = {
        "a": "the server reads configuration from the environment",
        "b": "the embedder provider is gemini",
        "c": "the server starts and the server stops",
    }
    scores = Bm25(docs).score("gemini embedder")
    assert max(scores, key=scores.get) == "b"


def test_a_term_present_everywhere_scores_far_below_a_rare_one():
    ubiquitous = Bm25({"a": "server server", "b": "server", "c": "server"}).score("server")
    rare = Bm25({"a": "gemini gemini", "b": "unrelated", "c": "also unrelated"}).score("gemini")
    assert max(ubiquitous.values()) < 0.5 * max(rare.values())


def test_scores_are_bounded_to_the_unit_interval_on_an_absolute_scale():
    # Absolute, not min-max: the best candidate must NOT be forced to 1.0, because
    # the ranker sums this against the memory layer's absolute embedding score.
    docs = {"a": "gemini gemini gemini", "b": "unrelated"}
    scores = Bm25(docs).score("gemini")
    assert 0.0 <= min(scores.values()) <= max(scores.values()) <= 1.0
    assert scores["b"] == 0.0
    assert 0.0 < scores["a"] < 1.0


def test_a_weak_match_does_not_score_like_a_strong_one_just_because_it_is_the_best():
    weak = Bm25({"a": "one passing mention of gemini here", "b": "nothing"}).score("gemini")
    strong = Bm25({"a": "gemini gemini gemini gemini", "b": "nothing"}).score("gemini")
    assert weak["a"] < strong["a"]


def test_an_empty_query_scores_everything_zero_rather_than_raising():
    scores = Bm25({"a": "x"}).score("")
    assert scores == {"a": 0.0}


def test_an_empty_corpus_does_not_raise():
    assert Bm25({}).score("anything") == {}


def test_every_document_gets_a_score_even_when_it_matches_nothing():
    # A missing key would read as "not retrieved" downstream instead of "scored zero".
    scores = Bm25({"a": "gemini", "b": "nothing in common"}).score("gemini")
    assert set(scores) == {"a", "b"}
