"""Token counting for the budget.

`tiktoken` is optional on purpose: it encodes for OpenAI models, and this stack
runs on Gemini. Making it a hard dependency would tie the budget to the wrong
tokenizer while looking authoritative. The counter names itself so a budget
report can say how it measured.
"""

from __future__ import annotations

import math
from typing import Protocol


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """~4 characters per token. Never reports zero for non-empty text.

    A zero-cost item would slip past every budget check.
    """

    name = "heuristic"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / 4))


class TiktokenCounter:
    def __init__(self, encoding_name: str, encoding) -> None:
        self.name = f"tiktoken:{encoding_name}"
        self._encoding = encoding

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(self._encoding.encode(text)))


def get_token_counter(encoding: str = "cl100k_base") -> TokenCounter:
    """Best available counter. Never raises: a missing encoder falls back."""
    try:
        import tiktoken

        return TiktokenCounter(encoding, tiktoken.get_encoding(encoding))
    except Exception:
        return HeuristicTokenCounter()
