"""BM25 relevance over the candidate set.

Handoff section 12 says not to rely on embedding similarity alone. This is the
lexical leg. It also keeps the file layers free of an embedding call per chunk:
memory items already arrive with the server's similarity score, and the ranker
uses that instead of recomputing.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens. Underscores are kept so SNAKE_CASE identifiers survive."""
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


class Bm25:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids = list(docs)
        self.tokens = {doc_id: tokenize(text) for doc_id, text in docs.items()}
        self.lengths = {doc_id: len(tokens) for doc_id, tokens in self.tokens.items()}
        self.counts = {doc_id: Counter(tokens) for doc_id, tokens in self.tokens.items()}
        self.n = len(self.doc_ids)
        self.avg_len = (sum(self.lengths.values()) / self.n) if self.n else 0.0
        self.doc_freq: Counter[str] = Counter()
        for counter in self.counts.values():
            self.doc_freq.update(counter.keys())

    def _idf(self, term: str) -> float:
        # Robertson/Sparck-Jones idf, floored at zero so a term present in every
        # document contributes nothing rather than a negative score.
        df = self.doc_freq.get(term, 0)
        if df == 0:
            return 0.0
        return max(0.0, math.log(1.0 + (self.n - df + 0.5) / (df + 0.5)))

    def score(self, query: str) -> dict[str, float]:
        """Score every document into [0, 1] on an ABSOLUTE scale.

        Deliberately not min-max normalised. Min-max would force the best
        candidate to 1.0 even when nothing matched, and the ranker then sums
        this against the memory layer's embedding similarity, which is
        absolute. Two components on different bases in one weighted sum give a
        total that is right only by luck. `1 - exp(-raw)` keeps the value
        bounded while staying comparable across queries and corpora.

        Every document gets a key: a missing one would read downstream as
        "not retrieved" rather than "scored zero".
        """
        if not self.n:
            return {}
        terms = tokenize(query)
        if not terms:
            return {doc_id: 0.0 for doc_id in self.doc_ids}

        raw: dict[str, float] = {}
        for doc_id in self.doc_ids:
            counts = self.counts[doc_id]
            length = self.lengths[doc_id] or 1
            total = 0.0
            for term in terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                denominator = freq + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1))
                total += self._idf(term) * (freq * (self.k1 + 1)) / denominator
            raw[doc_id] = total

        return {doc_id: 1.0 - math.exp(-value) for doc_id, value in raw.items()}
