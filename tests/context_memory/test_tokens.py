from src.context.tokens import HeuristicTokenCounter, get_token_counter


def test_heuristic_counter_is_monotonic_in_length():
    counter = HeuristicTokenCounter()
    assert counter.count("a" * 400) > counter.count("a" * 100)


def test_heuristic_counter_never_returns_zero_for_non_empty_text():
    # A zero-cost item would slip past every budget check.
    assert HeuristicTokenCounter().count("x") >= 1


def test_empty_text_costs_nothing():
    assert HeuristicTokenCounter().count("") == 0


def test_get_token_counter_falls_back_when_the_encoder_is_missing():
    counter = get_token_counter(encoding="definitely-not-an-encoding")
    assert counter.count("hello world") >= 1
    assert counter.name == "heuristic"


def test_the_counter_names_itself_so_a_budget_report_can_say_how_it_measured():
    assert get_token_counter().name in {"heuristic", "tiktoken:cl100k_base"}
