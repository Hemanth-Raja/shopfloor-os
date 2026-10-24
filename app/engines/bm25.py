"""BM25 (Okapi) keyword ranking, standard library only, plus reciprocal rank fusion (RRF). No model calls.

Clause retrieval fuses two independent rankings: H-MEM's hybrid ranking and BM25 over the clause text. RRF
(score = sum 1 / (k + rank)) rewards clauses both agree on and is robust to the two scales being different.
"""

from __future__ import annotations

import math
import re
from collections import Counter

TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "at", "on", "for", "by", "with", "is", "be", "shall", "any",
        "that", "this", "it", "as", "are", "from", "not", "no"}


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN.findall((text or "").lower().replace("_", " ")) if t not in STOP]


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {d: Counter(tokens(t)) for d, t in docs.items()}
        self.len = {d: sum(c.values()) for d, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 0.0
        df = Counter(w for c in self.tf.values() for w in c)
        n = len(self.tf)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def score(self, query: str, doc: str) -> float:
        tf, dl, s = self.tf.get(doc, Counter()), self.len.get(doc, 0), 0.0
        for w in set(tokens(query)):
            f = tf.get(w, 0)
            if f:
                s += self.idf.get(w, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avg or 1)))
        return s

    def rank(self, query: str, docs) -> list[str]:
        scored = [(self.score(query, d), d) for d in docs]
        return [d for s, d in sorted(scored, key=lambda x: (-x[0], x[1]))]


def rrf(*rankings: list[str], k: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for ranking in rankings:
        for i, d in enumerate(ranking):
            score[d] = score.get(d, 0.0) + 1.0 / (k + i + 1)
    first = {d: min(r.index(d) if d in r else 1e9 for r in rankings) for d in score}
    return sorted(score, key=lambda d: (-score[d], first[d], d))
