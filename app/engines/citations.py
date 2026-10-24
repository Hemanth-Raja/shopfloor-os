"""Clause citations: verbatim OSHA 29 CFR 1910 excerpts and plant SOPs, indexed in Meterless H-MEM.

Every clause is an H-MEM record. Its source is the clause reference, and its tags name the safety rules
(rule:R-CROSSING) and incident kinds (kind:spill) it governs. Retrieval is H-MEM's hybrid ranking, limited
to clauses tagged for the rule or kind at hand, so:
- the safety critic attaches the exact clause to every violation;
- the planner is shown the clauses relevant to the incident and must cite them by reference.

OSHA excerpts come from the eCFR (public domain), in data/seed/osha_1910_excerpts.json. The SOPs in
data/seed/plant_sops.json are synthetic, written for this fictional plant.
"""

from __future__ import annotations

import json
import itertools
from functools import lru_cache
from pathlib import Path

from app.engines.hmem import HMEM

SEED = Path(__file__).resolve().parents[2] / "data" / "seed"
_INDEX_IDS = itertools.count(1)
# Which OSHA clauses govern which of our safety rules
RULE_CLAUSES = {
    "R-CROSSING": ["29 CFR 1910.178(n)(4)"],
    "R-SAFE-SPEED": ["29 CFR 1910.178(n)(8)"],
    "R-PED-CLEAR": ["29 CFR 1910.178(m)(1)"],
    "R-TRAIN": ["29 CFR 1910.178(l)(1)(i)"],
    "R-SURFACE": ["29 CFR 1910.22(a)(1)", "29 CFR 1910.22(a)(2)", "29 CFR 1910.22(a)(3)", "29 CFR 1910.22(d)(2)"],
    "R-LOTO": ["29 CFR 1910.147(c)(1)", "29 CFR 1910.147(c)(4)(i)"],
    "R-GUARD": ["29 CFR 1910.212(a)(1)", "29 CFR 1910.212(a)(3)(ii)"],
    "R-PRESS": ["29 CFR 1910.217(c)(1)(i)"],
    "R-ROBOT-CELL": ["29 CFR 1910.212(a)(1)"],
    "R-NEAR-MISS": ["29 CFR 1910.178(m)(1)"],
    "R-HAZARD-ZONE": ["29 CFR 1910.147(c)(1)"],
}
KIND_RULES = {"part_shortage": ["R-CROSSING", "R-SAFE-SPEED", "R-TRAIN"], "agv_fault": ["R-CROSSING", "R-PED-CLEAR"],
              "overheat": ["R-LOTO", "R-GUARD"], "spill": ["R-SURFACE"],
              "robot_cell_fault": ["R-LOTO", "R-ROBOT-CELL", "R-GUARD"], "torque_fault": ["R-TORQUE-QA"],
              "near_miss": ["R-NEAR-MISS", "R-CROSSING"], "quality_defect": ["R-CONTAIN"]}


class CitationIndex:
    def __init__(self, osha: list[dict] | None = None, sops: list[dict] | None = None):
        if osha is None:
            path = SEED / "osha_1910_excerpts.json"
            osha = json.loads(path.read_text()) if path.exists() else []
        if sops is None:
            path = SEED / "plant_sops.json"
            sops = json.loads(path.read_text()) if path.exists() else []
        self.uid = next(_INDEX_IDS)  # cache key part: unique per index, never reused
        self.hmem = HMEM(clock=lambda: 0.0, actor="citations")
        self.by_ref: dict[str, dict] = {}
        for e in osha:
            rules = [r for r, refs in RULE_CLAUSES.items() if e["ref"] in refs]
            kinds = sorted({k for k, rs in KIND_RULES.items() if set(rs) & set(rules)})
            self._add(e["ref"], e["title"], e["text"], "osha", rules, kinds)
        for s in sops:
            self._add(s["ref"], s["title"], s["text"], "sop", s["rules"], s["kinds"])

    def _add(self, ref: str, title: str, text: str, doc: str, rules: list[str], kinds: list[str]) -> None:
        tags = [f"doc:{doc}"] + [f"rule:{r}" for r in rules] + [f"kind:{k}" for k in kinds]
        self.hmem.add(f"{title} {text}", source=ref, type="factual", layer="long_term", confidence=1.0, tags=tags,
                      provenance={"origin": "ecfr" if doc == "osha" else "plant_sop", "label": ref})
        self.by_ref[ref] = {"ref": ref, "title": title, "text": text, "doc": doc}

    def _ranked(self, query: str, where, top_n: int) -> list[dict]:
        """Tag filter first (rule/kind), then two rankings fused with RRF: H-MEM's hybrid ranking and BM25."""
        hits = self.hmem.query(query, threshold=0, top_n=64, where=where)["memories"]
        by_hmem = [h["memory"]["source"] for h in hits]
        if not hasattr(self, "_bm25") or self._bm25_size != len(self.by_ref):
            from app.engines.bm25 import BM25
            self._bm25 = BM25({r: f"{c['title']} {c['text']}" for r, c in self.by_ref.items()})
            self._bm25_size = len(self.by_ref)
        from app.engines.bm25 import rrf
        fused = rrf(by_hmem, self._bm25.rank(query, by_hmem))
        return [self.by_ref[r] for r in fused[:top_n]]

    def for_rule(self, rule_id: str, detail: str = "", top_n: int = 2) -> list[dict]:
        """Clauses for a violated rule: the OSHA clause first, then the plant SOP (cached: the index is fixed)."""
        from app.cache import cache
        return cache("clauses", 3600).get(("rule", self.uid, rule_id, detail, top_n),
                                          lambda: self._for_rule(rule_id, detail, top_n))

    def _for_rule(self, rule_id: str, detail: str = "", top_n: int = 2) -> list[dict]:
        tag = f"rule:{rule_id}"
        osha = self._ranked(detail or rule_id, lambda m: tag in m["tags"] and "doc:osha" in m["tags"], 1)
        sop = self._ranked(detail or rule_id, lambda m: tag in m["tags"] and "doc:sop" in m["tags"], max(1, top_n - 1))
        return (osha + sop)[:top_n]

    def for_incident(self, kind: str, description: str, top_n: int = 4) -> list[dict]:
        from app.cache import cache
        return cache("clauses", 3600).get(("kind", self.uid, kind, description, top_n),
                                          lambda: self._for_incident(kind, description, top_n))

    def _for_incident(self, kind: str, description: str, top_n: int = 4) -> list[dict]:
        tag = f"kind:{kind}"
        return self._ranked(description, lambda m: tag in m["tags"], top_n)

    def resolve(self, refs: list[str]) -> list[dict]:
        """Full clauses for the references a plan cites; unknown references are dropped."""
        return [self.by_ref[r] for r in refs if r in self.by_ref]


@lru_cache(maxsize=1)
def default_index() -> CitationIndex:
    return CitationIndex()
