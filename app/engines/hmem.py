"""Meterless H-MEM engine (hierarchical memory), implemented in Python.

A Python port of the Meterless H-MEM specification (github.com/Meterless/Meterless, engines/hmem,
Apache-2.0). The spec, conformance runner and vectors, and the TypeScript reference are kept in
data/raw/meterless/hmem/. The conformance vectors are ported to tests/test_hmem.py.

- Three layers: short_term, working and long_term. Every record carries a source; no source, no record.
- Retrieval uses 8-signal hybrid ranking: semantic, keyword, tag, domain, entity, layer, recency and a
  superseded penalty. The result is multiplied by confidence.
- Feedback deltas: helpful +0.05 and +1 access, not_helpful -0.03, wrong -0.20 plus a review tag; clamped to [0, 1].
- The trust ledger is append-only: create, read, feedback, update, conflict and sleep actions, with
  previous and new state.
- Sleep is preview-first, with a backup before execute. Guardrails protect derived, superseding, hub
  and referenced records.
- Conflicts: opposing phrase pairs on similar content. Auto-resolve marks the loser superseded and never
  deletes it.
- Dreaming proposes insights and invariants. Nothing is created until a person approves, and approved
  records are long_term with derivedFrom lineage.

Standard library only. The embedding is the spec's deterministic 64-dimension hashed bag of words.
"""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

DAY_MS = 24 * 60 * 60 * 1000
FEEDBACK_DELTAS = {"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.2}
DEFAULTS = {"retrievalTopN": 5, "retrievalThreshold": 0.35, "consolidationDays": 7, "archiveMinDays": 30,
            "archiveMaxAccess": 2, "synthesisCosine": 0.82, "dreamClusterMinSize": 2, "dreamRelatedness": 0.35,
            "maxDreamProposalsPerType": 8, "conflictAutoResolveGate": 0.7, "entityCap": 10, "relatedCap": 5,
            "recencyDays": 14}
LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
HUB_THRESHOLD = 3
DIM = 64
TECH_TERMS = ["react", "python", "typescript", "javascript", "rust", "sqlite", "postgres", "indexeddb", "node",
              "docker", "graphql", "redis", "webrtc", "tauri", "electron", "llm", "embedding"]
DOMAIN_KEYWORDS = {
    "tech": ["code", "bug", "api", "database", "deploy", "framework", "library", "server", *TECH_TERMS],
    "work": ["meeting", "deadline", "client", "project", "sprint", "review", "report"],
    "personal": ["family", "hobby", "prefer", "like", "dislike", "morning", "evening"],
}
OPPOSING_PAIRS = [(r"\balways\b", r"\bnever\b"), (r"\benable[ds]?\b", r"\bdisable[ds]?\b"),
                  (r"\bprefer[s]?\b", r"\bavoid[s]?|dislike[s]?\b"), (r"\bon\b", r"\boff\b"), (r"\byes\b", r"\bno\b")]
CORRECTION = re.compile(r"\b(actually|instead|no longer|not anymore|changed to|correction)\b", re.I)


def clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


# ---------------------------------------------------------------- embeddings (spec: deterministic hashed BoW)

def tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", re.sub(r"[^a-z0-9\s]", " ", text.lower())) if len(t) > 1]


def _fnv1a(s: str) -> int:
    h = 0x811C9DC5
    data = s.encode("utf-16-le")
    for i in range(0, len(data), 2):  # JS charCodeAt: iterate UTF-16 code units
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for token in tokenize(text):
        h = _fnv1a(token)
        sign = 1 if (h >> 8) % 2 == 0 else -1
        vec[h % DIM] += sign * (1 + ((h >> 16) % 8) / 8)
    norm = math.sqrt(sum(v * v for v in vec))
    return vec if norm == 0 else [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both unit length


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


def _snapshot(r: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in r.items() if k != "embedding"}


# ---------------------------------------------------------------- services

class TrustLedger:
    def __init__(self, clock: Callable[[], float], persist_dir: Path | None = None):
        self.clock, self.entries = clock, []
        self.file = persist_dir / "ledger.jsonl" if persist_dir else None
        if self.file and self.file.exists():
            self.entries = [json.loads(l) for l in self.file.read_text().splitlines() if l]

    def log(self, memory_id: str, action: str, actor: str, **extra) -> dict:
        entry = {"memoryId": memory_id, "action": action, "actor": actor, "timestamp": self.clock(),
                 **{k: v for k, v in extra.items() if v is not None}}
        self.entries.append(entry)
        if self.file:
            with self.file.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    def history(self, memory_id: str) -> list[dict]:
        return [e for e in self.entries if e["memoryId"] == memory_id]

    def all(self) -> list[dict]:
        return self.entries


class MemoryStore:
    def __init__(self, clock, ledger: TrustLedger, actor: str, persist_dir: Path | None = None):
        self.clock, self.ledger, self.actor = clock, ledger, actor
        self.records: dict[str, dict] = {}
        self.seq = 0
        self.file = persist_dir / "state.json" if persist_dir else None
        if self.file and self.file.exists():
            saved = json.loads(self.file.read_text())
            self.seq = saved["seq"]
            self.records = {r["id"]: r for r in saved["records"]}

    def persist(self) -> None:
        if self.file:
            recs = [self.records[k] for k in sorted(self.records)]
            self.file.write_text(json.dumps({"seq": self.seq, "records": recs}, indent=1))

    def add(self, content: str, source: str | None, type: str = "general", layer: str = "working",
            tags: list[str] | None = None, confidence: float | None = None, provenance: dict | None = None,
            supersedes: str | None = None, derived_from: list[str] | None = None, **extra) -> dict:
        if not source or not str(source).strip():
            raise ValueError("H-MEM: a memory record with no source must be rejected by construction")
        now = self.clock()
        self.seq += 1
        tags = list(tags or [])
        domain = self.classify_domain(content, tags, type)
        record = {"id": f"mem-{self.seq}", "content": content, "type": type, "layer": layer, "timestamp": now,
                  "lastAccessed": now, "accessCount": 0, "tags": tags, "embedding": embed(content),
                  "confidence": clamp01(0.7 if confidence is None else confidence), "source": source,
                  "provenance": provenance or {"origin": "manual", "learnedAt": now, "label": source},
                  "domain": domain, "namespace": self._namespace(domain, type),
                  "entities": self.extract_entities(content), "relatedTo": [], "supersedes": supersedes,
                  "supersededBy": None, "derivedFrom": derived_from, **extra}
        self._discover_relationships(record)
        self.records[record["id"]] = record
        self.ledger.log(record["id"], "create", self.actor, newState=_snapshot(record))
        self.persist()
        return record

    def get(self, id_: str) -> dict | None:
        return self.records.get(id_)

    def all(self) -> list[dict]:
        return list(self.records.values())

    def update(self, id_: str, **patch) -> dict:
        rec = self._must(id_)
        prev = _snapshot(rec)
        rec.update(patch)
        self.ledger.log(id_, "update", self.actor, previousState=prev, newState=_snapshot(rec))
        self.persist()
        return rec

    def delete(self, id_: str, reason: str) -> None:
        rec = self._must(id_)
        del self.records[id_]
        self.ledger.log(id_, "delete", self.actor, previousState=_snapshot(rec), details={"reason": reason})
        self.persist()

    def touch(self, id_: str) -> None:
        rec = self._must(id_)
        rec["lastAccessed"] = self.clock()
        rec["accessCount"] += 1

    def promote(self, id_: str, layer: str) -> None:
        rec = self._must(id_)
        prev = _snapshot(rec)
        rec["layer"] = layer
        self.ledger.log(id_, "promote", self.actor, previousState=prev, newState=_snapshot(rec))
        self.persist()

    def snapshot_all(self) -> dict:
        return {"records": copy.deepcopy(list(self.records.values())), "seq": self.seq}

    def restore_all(self, records: list[dict], seq: int) -> None:
        self.records = {r["id"]: copy.deepcopy(r) for r in records}
        self.seq = seq
        self.persist()

    def _must(self, id_: str) -> dict:
        if id_ not in self.records:
            raise KeyError(f"H-MEM: unknown memory id {id_}")
        return self.records[id_]

    def classify_domain(self, content: str, tags: list[str], type: str) -> str:
        for t in tags:
            if t.startswith("project:"):
                return t[len("project:"):]
            if t.startswith("tech:"):
                return "tech"
        tokens = set(tokenize(content))
        best, best_score = "general", 0
        for domain, kws in DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in kws if kw in tokens)
            if domain == "personal" and type in ("personal", "preference"):
                score += 1
            if score > best_score:
                best, best_score = domain, score
        return best if best_score > 0 else "general"

    @staticmethod
    def _namespace(domain: str, type: str) -> str:
        bucket = "profile" if type in ("preference", "personal") else "facts" if type == "factual" else "notes"
        return f"{domain}/{bucket}"

    @staticmethod
    def extract_entities(content: str) -> list[str]:
        out: list[str] = []
        for t in tokenize(content):
            if t in TECH_TERMS and t not in out:
                out.append(t)
        for m in re.finditer(r"\b([A-Z][a-zA-Z]{2,})\b", content):
            norm = m.group(1).lower()
            if norm not in ("the", "this", "that", "with") and norm not in out:
                out.append(norm)
        return out[:DEFAULTS["entityCap"]]

    def _discover_relationships(self, record: dict) -> None:
        for other in self.records.values():
            if len(record["relatedTo"]) >= DEFAULTS["relatedCap"]:
                break
            shared = [e for e in other["entities"] if e in record["entities"]]
            sim = cosine(record["embedding"], other["embedding"])
            if not record["supersedes"] and sim > 0.85 and CORRECTION.search(record["content"]):
                record["supersedes"] = other["id"]
                continue
            if shared and other["domain"] == record["domain"]:
                record["relatedTo"].append(other["id"])
                if len(other["relatedTo"]) < DEFAULTS["relatedCap"] and record["id"] not in other["relatedTo"]:
                    other["relatedTo"].append(record["id"])


class Retrieval:
    def __init__(self, store: MemoryStore, ledger: TrustLedger, clock, actor: str):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor

    def _score(self, m: dict, q_emb, q_tokens, q_entities, q_domain) -> float:
        semantic = clamp01(cosine(q_emb, m["embedding"]))
        keyword = jaccard(q_tokens, tokenize(m["content"]))
        tag = jaccard(q_tokens, [t.lower() for t in m["tags"]])
        domain = 1 if m["domain"] == q_domain else 0
        entity = jaccard(q_entities, m["entities"])
        layer = LAYER_WEIGHT[m["layer"]]
        recency = math.exp(-((self.clock() - m["lastAccessed"]) / DAY_MS) / DEFAULTS["recencyDays"])
        superseded = 1 if m.get("supersededBy") else 0
        raw = (0.35 * semantic + 0.2 * keyword + 0.1 * tag + 0.1 * domain + 0.15 * entity + 0.05 * layer
               + 0.05 * recency - 0.2 * superseded)
        return clamp01(raw) * m["confidence"]

    def query(self, query: str, top_n: int | None = None, threshold: float | None = None,
              where: Callable[[dict], bool] | None = None) -> dict:
        q_emb, q_tokens = embed(query), tokenize(query)[:8]
        q_entities = self.store.extract_entities(query)
        q_domain = self.store.classify_domain(query, [], "general")
        threshold = DEFAULTS["retrievalThreshold"] if threshold is None else threshold
        top_n = DEFAULTS["retrievalTopN"] if top_n is None else top_n
        scored = [{"memory": m, "relevance": s} for m in self.store.all() if (not where or where(m))
                  for s in [self._score(m, q_emb, q_tokens, q_entities, q_domain)] if s >= threshold]
        scored.sort(key=lambda r: (-r["relevance"], r["memory"]["id"]))
        top = scored[:top_n]
        for item in top:
            self.store.touch(item["memory"]["id"])
            self.ledger.log(item["memory"]["id"], "read", self.actor, details={"query": query})
        return {"memories": top, "trace": {"retrievalReason": f'hybrid ranking for "{query}" (domain: {q_domain})',
                                           "scores": {t["memory"]["id"]: round(t["relevance"], 4) for t in top}}}

    def feedback(self, memory_id: str, kind: str) -> None:
        m = self.store.get(memory_id)
        if m is None:
            raise KeyError(f"H-MEM: unknown memory id {memory_id}")
        prev = _snapshot(m)
        if kind == "helpful":
            m["accessCount"] += 1
        m["confidence"] = clamp01(round(m["confidence"] + FEEDBACK_DELTAS[kind], 10))
        if kind == "wrong" and "review" not in m["tags"]:
            m["tags"].append("review")
        self.ledger.log(memory_id, "feedback", self.actor, previousState=prev, newState=_snapshot(m),
                        details={"kind": kind, "delta": FEEDBACK_DELTAS[kind]})
        self.store.persist()


class SleepCycle:
    def __init__(self, store: MemoryStore, ledger: TrustLedger, clock, actor: str):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.backups: dict[str, dict] = {}

    def preview(self) -> dict:
        now, recs = self.clock(), self.store.all()
        referenced = set()
        for m in recs:
            referenced.update(m["relatedTo"])
            if m.get("supersedes"):
                referenced.add(m["supersedes"])
            referenced.update(m.get("derivedFrom") or [])
        consolidate = [m["id"] for m in recs if m["layer"] == "short_term"
                       and now - m["lastAccessed"] >= DEFAULTS["consolidationDays"] * DAY_MS]
        archive = [m["id"] for m in recs
                   if now - m["timestamp"] >= DEFAULTS["archiveMinDays"] * DAY_MS
                   and m["accessCount"] <= DEFAULTS["archiveMaxAccess"] and m["id"] not in referenced
                   and not m.get("supersedes") and not (m.get("derivedFrom") or [])
                   and len(m["relatedTo"]) < HUB_THRESHOLD]
        synth, used = [], set()
        for i, a in enumerate(recs):
            if a["id"] in used:
                continue
            group = [a] + [b for b in recs[i + 1:] if b["id"] not in used
                           and cosine(a["embedding"], b["embedding"]) >= DEFAULTS["synthesisCosine"]]
            if len(group) >= 2:
                used.update(g["id"] for g in group)
                synth.append([g["id"] for g in group])
        return {"toConsolidate": consolidate, "toArchive": archive, "toSynthesize": synth}

    def execute(self, preview: dict | None = None) -> dict:
        plan = preview or self.preview()
        backup_id = f"backup-{len(self.backups) + 1}"
        self.backups[backup_id] = self.store.snapshot_all()
        log = [f"backup {backup_id} created"]
        for id_ in plan["toConsolidate"]:
            if self.store.get(id_):
                self.store.promote(id_, "long_term")
                self.ledger.log(id_, "sleep_consolidate", self.actor, details={"from": "short_term", "to": "long_term"})
                log.append(f"consolidated {id_}")
        for id_ in plan["toArchive"]:
            m = self.store.get(id_)
            if m:
                self.ledger.log(id_, "sleep_archive", self.actor, details={"accessCount": m["accessCount"]})
                self.store.delete(id_, "sleep archive")
                log.append(f"archived {id_}")
        synthesized = 0
        for group in plan["toSynthesize"]:
            members = [m for m in (self.store.get(i) for i in group) if m]
            if len(members) < 2:
                continue
            merged = self.store.add(content="Synthesis: " + " | ".join(m["content"] for m in members), type="factual",
                                    layer="long_term", tags=sorted({t for m in members for t in m["tags"]}),
                                    confidence=max(m["confidence"] for m in members), source="sleep:synthesis",
                                    provenance={"origin": "sleep_synthesis", "learnedAt": self.clock()},
                                    derived_from=[m["id"] for m in members])
            self.ledger.log(merged["id"], "sleep_synthesize", self.actor, details={"sources": group})
            for m in members:
                self.store.delete(m["id"], "merged by sleep synthesis")
            synthesized += 1
        return {"backupId": backup_id, "consolidated": len(plan["toConsolidate"]), "archived": len(plan["toArchive"]),
                "synthesized": synthesized, "actionLog": log}

    def restore(self, backup_id: str) -> None:
        b = self.backups[backup_id]
        self.store.restore_all(b["records"], b["seq"])
        self.ledger.log("*", "restore", self.actor, details={"backupId": backup_id})


class Conflicts:
    def __init__(self, store: MemoryStore, ledger: TrustLedger, clock, actor: str):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.found: list[dict] = []

    def scan(self) -> list[dict]:
        out, recs = [], self.store.all()
        for i, a in enumerate(recs):
            for b in recs[i + 1:]:
                c = self._check(a, b)
                if c:
                    out.append(c)
                    self.found.append(c)
                    self.ledger.log(c["memoryA"], "conflict_detected", self.actor,
                                    details={"with": c["memoryB"], "reason": c["reason"], "confidence": c["confidence"]})
        return out

    def _check(self, a: dict, b: dict) -> dict | None:
        if b["id"] in (a.get("supersedes"), a.get("supersededBy")) or a["id"] in (b.get("supersedes"), b.get("supersededBy")):
            return None
        sim = cosine(a["embedding"], b["embedding"])
        reason, confidence = "", 0.0
        for p, q in OPPOSING_PAIRS:
            if ((re.search(p, a["content"], re.I) and re.search(q, b["content"], re.I))
                    or (re.search(q, a["content"], re.I) and re.search(p, b["content"], re.I))) and sim > 0.5:
                reason, confidence = "opposing phrase pair on similar content", 0.5 + 0.3 * sim
        if not reason and sim > 0.8:
            na, nb = re.findall(r"\d+(?:\.\d+)?", a["content"]), re.findall(r"\d+(?:\.\d+)?", b["content"])
            if na and nb and na != nb:
                reason, confidence = "high-similarity conflicting numeric values", 0.55 + 0.25 * sim
        if not reason:
            return None
        if set(a["entities"]) & set(b["entities"]):
            confidence += 0.1
        if a["domain"] == b["domain"]:
            confidence += 0.05
        if b["id"] in a["relatedTo"]:
            confidence -= 0.1
        return {"id": f"conflict-{len(self.found) + 1}", "memoryA": a["id"], "memoryB": b["id"], "reason": reason,
                "confidence": clamp01(confidence), "resolved": False}

    def auto_resolve(self, conflict: dict) -> str:
        a, b = self.store.get(conflict["memoryA"]), self.store.get(conflict["memoryB"])
        now = max(1, self.clock())

        def score(m):
            return (0.3 * (m["lastAccessed"] / now) + 0.2 * min(1, m["accessCount"] / 10) + 0.25 * m["confidence"]
                    + 0.1 * min(1, len(m["content"]) / 200)
                    + 0.15 * (1 if m["provenance"].get("origin") == "user_correction" else 0.5))
        sa, sb = score(a), score(b)
        margin = abs(sa - sb) / max(sa, sb)
        if clamp01(conflict["confidence"] * 0.6 + margin * 0.8) < DEFAULTS["conflictAutoResolveGate"]:
            return "queued"
        winner, loser = (a, b) if sa >= sb else (b, a)
        prev = _snapshot(loser)
        loser["supersededBy"] = winner["id"]  # kept and penalised, never deleted
        conflict.update(resolved=True, resolution="keep_a" if winner is a else "keep_b")
        self.ledger.log(loser["id"], "conflict_resolved", self.actor, previousState=prev, newState=_snapshot(loser),
                        details={"conflictId": conflict["id"], "winner": winner["id"]})
        self.store.persist()
        return "resolved"


class Dreaming:
    def __init__(self, store: MemoryStore, ledger: TrustLedger, clock, actor: str):
        self.store, self.ledger, self.clock, self.actor = store, ledger, clock, actor
        self.proposals: dict[str, dict] = {}

    def _relatedness(self, a, b) -> float:
        return (0.5 * max(0.0, cosine(a["embedding"], b["embedding"])) + 0.2 * jaccard(a["tags"], b["tags"])
                + 0.1 * (1 if a["domain"] == b["domain"] else 0) + 0.2 * jaccard(a["entities"], b["entities"]))

    def dream(self) -> list[dict]:
        created: list[dict] = []

        def propose(**p):
            prop = {**p, "id": f"dream-{len(self.proposals) + 1}", "status": "proposed"}
            self.proposals[prop["id"]] = prop
            created.append(prop)
            self.ledger.log(prop["id"], "dream_proposed", self.actor, details={"type": p["type"], "derivedFrom": p["derivedFrom"]})

        recs, used = self.store.all(), set()
        clusters = []
        for seed in recs:
            if seed["id"] in used:
                continue
            group = [seed] + [o for o in recs if o["id"] != seed["id"] and o["id"] not in used
                              and self._relatedness(seed, o) >= DEFAULTS["dreamRelatedness"]]
            if len(group) >= DEFAULTS["dreamClusterMinSize"]:
                used.update(g["id"] for g in group)
                clusters.append(group)
        for cluster in clusters[:DEFAULTS["maxDreamProposalsPerType"]]:
            propose(type="insight", content=f"Across {len(cluster)} related memories: " + " + ".join(m["content"] for m in cluster),
                    derivedFrom=[m["id"] for m in cluster])
        prefs: dict[str, list[dict]] = {}
        for m in recs:
            if m["type"] in ("preference", "personal") or "event:user_correction" in m["tags"]:
                prefs.setdefault(m["domain"], []).append(m)
        for domain, group in list(prefs.items())[:DEFAULTS["maxDreamProposalsPerType"]]:
            if len(group) >= 2:
                propose(type="invariant", content=f"Stable pattern in {domain}: " + "; ".join(m["content"] for m in group),
                        derivedFrom=[m["id"] for m in group])
        return created

    def approve(self, proposal_id: str) -> dict | None:
        p = self.proposals.get(proposal_id)
        if not p or p["status"] != "proposed":
            raise KeyError(f"H-MEM: no pending proposal {proposal_id}")
        p["status"] = "approved"
        self.ledger.log(proposal_id, "dream_approved", self.actor, details={"type": p["type"]})
        return self.store.add(content=p["content"], type="factual" if p["type"] == "insight" else "preference",
                              layer="long_term", tags=[f"dream:{p['type']}"], confidence=0.75, source="dreaming",
                              provenance={"origin": "dream", "learnedAt": self.clock()}, derived_from=p["derivedFrom"])

    def reject(self, proposal_id: str) -> None:
        p = self.proposals[proposal_id]
        p["status"] = "rejected"
        self.ledger.log(proposal_id, "dream_rejected", self.actor, details={"type": p["type"]})


class HMEM:
    """Facade matching the spec: add, query, feedback plus store, ledger, sleep, dreaming, conflicts."""

    def __init__(self, clock: Callable[[], float] | None = None, actor: str = "system", persist_dir: str | Path | None = None):
        import time
        self.clock = clock or (lambda: time.time() * 1000)
        pdir = Path(persist_dir) if persist_dir else None
        if pdir:
            pdir.mkdir(parents=True, exist_ok=True)
        self.ledger = TrustLedger(self.clock, pdir)
        self.store = MemoryStore(self.clock, self.ledger, actor, pdir)
        self.retrieval = Retrieval(self.store, self.ledger, self.clock, actor)
        self.conflicts = Conflicts(self.store, self.ledger, self.clock, actor)
        self.sleep = SleepCycle(self.store, self.ledger, self.clock, actor)
        self.dreaming = Dreaming(self.store, self.ledger, self.clock, actor)

    def add(self, content: str, source: str | None, **kw: Any) -> dict:
        return self.store.add(content, source, **kw)

    def query(self, query: str, top_n: int | None = None, threshold: float | None = None, where=None) -> dict:
        return self.retrieval.query(query, top_n, threshold, where)

    def feedback(self, memory_id: str, kind: str) -> None:
        self.retrieval.feedback(memory_id, kind)
