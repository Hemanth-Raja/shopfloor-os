"""Meterless World Model engine, implemented in Python.

A Python port of the Meterless World Model specification (github.com/Meterless/Meterless,
engines/world-model, Apache-2.0). The spec, conformance runner and TypeScript reference are kept
for reference in data/raw/meterless/world-model/. Section numbers below refer to its AGENTS.md.

The engine is a persistent, queryable model of entities, the contexts they appear in, and the
relationships between them:

- Event-sourced. State is a fold over an append-only event log; views are projections (s.1, s.12.2).
- Stable ids derived from content: `type:system:externalId`, or `ent_` + sha256 of the normalised
  name (docs/stable-ids.md).
- Every write carries provenance (kind + at); no provenance, no write.
- Bitemporal edges. A superseding relate closes the old edge instead of deleting it.
- Banded name resolution: >= 0.85 auto-merges, [0.82, 0.85) queues for review, below is new.
- Idempotent ingest. The same batch twice gives an empty diff (s.4.1).
- Every mutation writes an ordered audit entry.

Standard library only. Python method names are snake_case versions of the spec's names
(upsertEntity -> upsert_entity, stateShapeCanonical -> state_shape_canonical, and so on).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

AUTO_MERGE_AT = 0.85
REVIEW_LOWER = 0.82
CLUSTER_JACCARD = 0.5
HOUR_MS = 60 * 60 * 1000
EPOCH_ISO = "1970-01-01T00:00:00.000Z"


class ProvenanceError(ValueError):
    """Raised when a write arrives without provenance (kind + at)."""


# ---------------------------------------------------------------- stable ids

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_scalar(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # JSON.stringify(87.0) is "87"
    if isinstance(value, (datetime,)):
        return json.dumps(iso(value))
    return json.dumps(value, ensure_ascii=False)


def canonical_json(value: Any) -> str:
    """Deterministic JSON: object keys sorted, None-valued members dropped (JS `undefined`)."""
    if isinstance(value, dict):
        keys = sorted(k for k, v in value.items() if v is not None)
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical_json(value[k]) for k in keys) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    return _json_scalar(value)


_COMBINING = re.compile("[̀-ͯ]")
_ZERO_WIDTH = re.compile("[​-‍﻿]")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """NFKD -> strip combining marks -> strip zero-width -> lowercase -> collapse whitespace -> trim.

    Never abbreviates or stems: "Acme Corp" and "Acme Corporation" stay different."""
    text = unicodedata.normalize("NFKD", name)
    text = _ZERO_WIDTH.sub("", _COMBINING.sub("", text)).lower()
    return _SPACES.sub(" ", text).strip()


def external_id(type_: str, system: str, ext_id: str) -> str:
    return f"{type_}:{system}:{ext_id}"


def entity_id_from_name(type_: str, name: str) -> str:
    return "ent_" + sha256_hex(f"{type_}:{normalize_name(name)}")[:16]


def context_id_from_attrs(type_: str, attrs: dict, parent: str | None = None) -> str:
    return "ctx_" + sha256_hex(f"{type_}:{canonical_json(attrs)}:{parent or ''}")[:16]


def relationship_id(from_: str, type_: str, to: str, context: str | None = None, valid_from: str | None = None) -> str:
    return "rel_" + sha256_hex(f"{from_}|{type_}|{to}|{context or ''}|{valid_from or ''}")[:16]


def fact_id(about: str, predicate: str, value: Any, asserted_at: str, source_kind: str, source_url: str | None = None) -> str:
    return "fact_" + sha256_hex(
        f"{about}|{predicate}|{canonical_json(value)}|{asserted_at}|{source_kind}|{source_url or ''}")[:16]


def iso(value: str | datetime) -> str:
    """ISO-8601 with milliseconds and Z. Naive datetimes are taken as already being on the world's clock."""
    if isinstance(value, str):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(value: str) -> float:
    return _parse_iso(value).timestamp() * 1000


def _iso_from_ms(ms: float) -> str:
    return iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


# ---------------------------------------------------------------- resolution

def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = curr
    return prev[len(b)]


def name_similarity(raw_a: str, raw_b: str) -> float:
    a, b = normalize_name(raw_a), normalize_name(raw_b)
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1 - levenshtein(a, b) / longest


def decide(similarity: float, auto_merge_at: float = AUTO_MERGE_AT, review_lower: float = REVIEW_LOWER) -> str:
    """Merge banding: 0.85 exactly auto-merges, 0.82 exactly queues for review."""
    if similarity >= auto_merge_at:
        return "auto-merge"
    if similarity >= review_lower:
        return "review"
    return "new"


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


# ---------------------------------------------------------------- event store

class EventStore:
    """Append-only event log plus its fold. Optional JSON persistence at <dir>/.world/<namespace>.json."""

    def __init__(self, namespace: str, storage: str = "memory", directory: str | Path | None = None):
        self.events: list[dict] = []
        self.audit: list[dict] = []
        self.observations: dict[str, dict] = {}
        self.stories: dict[str, dict] = {}
        self.scores: list[dict] = []
        self.merge_queue: list[dict] = []
        self.state = {"entities": {}, "contexts": {}, "relationships": {}, "facts": {}, "aliases": {}}
        self._seq = 0
        self._audit_seq = 0
        self._subscribers: list[Callable[[dict], None]] = []
        self._file: Path | None = None
        if storage == "local":
            folder = Path(directory or Path.cwd()) / ".world"
            folder.mkdir(parents=True, exist_ok=True)
            self._file = folder / f"{namespace}.json"
            if self._file.exists():
                saved = json.loads(self._file.read_text(encoding="utf-8"))
                self.audit = saved.get("audit", [])
                self._audit_seq = self.audit[-1]["seq"] if self.audit else 0
                self.observations = dict(saved.get("observations", []))
                self.stories = dict(saved.get("stories", []))
                self.scores = saved.get("scores", [])
                self.merge_queue = saved.get("mergeQueue", [])
                for event in saved.get("events", []):
                    self._apply(event, is_new=False)

    def append(self, kind: str, payload: dict, at: str) -> dict:
        event = {"id": f"evt_{self._seq + 1}", "seq": self._seq + 1, "kind": kind,
                 "payload": copy.deepcopy(payload), "schemaVersion": 1, "at": at}
        self._apply(event, is_new=True)
        return event

    def _apply(self, event: dict, is_new: bool) -> None:
        self._seq = max(self._seq, event["seq"])
        self.events.append(event)
        self.fold(event, self.state)
        if is_new:
            self.persist()
            for fn in list(self._subscribers):
                fn(event)

    @staticmethod
    def fold(event: dict, state: dict) -> None:
        kind, p = event["kind"], event["payload"]
        if kind == "upsertEntity":
            existing = state["entities"].get(p["id"])
            if existing:
                existing["attrs"] = {**existing["attrs"], **p["attrs"]}
                existing["updatedAt"] = p["updatedAt"]
                existing["source"] = p["source"]
                name = p.get("name")
                if name and name not in existing["aliases"]:
                    existing["aliases"].append(name)
                if name:
                    existing["name"] = name
            else:
                state["entities"][p["id"]] = copy.deepcopy(p)
        elif kind == "upsertContext":
            existing = state["contexts"].get(p["id"])
            if existing:
                existing["attrs"] = {**existing["attrs"], **p["attrs"]}
                existing["updatedAt"] = p["updatedAt"]
            else:
                state["contexts"][p["id"]] = copy.deepcopy(p)
        elif kind == "relate":
            # A new open edge with the same (from, type, context) but a different target
            # closes the previous open edge.
            for other in state["relationships"].values():
                if (other["from"] == p["from"] and other["type"] == p["type"]
                        and (other.get("context") or "") == (p.get("context") or "")
                        and other["to"] != p["to"] and not other.get("validTo") and not other.get("supersededBy")):
                    other["validTo"] = p.get("validFrom") or p["createdAt"]
                    other["supersededBy"] = p["id"]
            state["relationships"].setdefault(p["id"], copy.deepcopy(p))
        elif kind == "assertFact":
            state["facts"].setdefault(p["id"], copy.deepcopy(p))
        elif kind == "alias":
            state["aliases"][p["aliasId"]] = p["primaryId"]

    def fold_until(self, at: str) -> dict:
        state = {"entities": {}, "contexts": {}, "relationships": {}, "facts": {}, "aliases": {}}
        for event in self.events:
            if event["at"] <= at:
                self.fold(event, state)
        return state

    def resolve_alias(self, id_: str, aliases: dict | None = None) -> str:
        aliases = self.state["aliases"] if aliases is None else aliases
        seen, cur = set(), id_
        while cur in aliases and cur not in seen:
            seen.add(cur)
            cur = aliases[cur]
        return cur

    def log_audit(self, kind: str, actor: str, summary: str, event_id: str | None = None,
                  run_id: str | None = None, at: str | None = None) -> None:
        self._audit_seq += 1
        self.audit.append({"seq": self._audit_seq, "at": at or EPOCH_ISO, "kind": kind, "actor": actor,
                           "summary": summary, "eventId": event_id, "runId": run_id})
        self.persist()

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    def persist(self) -> None:
        if not self._file:
            return
        data = {"events": self.events, "audit": self.audit, "observations": list(self.observations.items()),
                "stories": list(self.stories.items()), "scores": self.scores, "mergeQueue": self.merge_queue}
        self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- ingest pipeline (s.4.1)

def world_state_shape(store: EventStore) -> dict:
    """The idempotency diff surface: everything derived from observations, sorted."""
    by_id = lambda d: [d[k] for k in sorted(d)]  # noqa: E731
    return {"entities": by_id(store.state["entities"]), "contexts": by_id(store.state["contexts"]),
            "relationships": by_id(store.state["relationships"]), "facts": by_id(store.state["facts"]),
            "observations": [store.observations[k] for k in sorted(store.observations)],
            "stories": by_id(store.stories)}


def run_ingest_pipeline(store: EventStore, observations: list[dict],
                        upsert: Callable[[dict], tuple[str, list[str]]], timer: Callable[[], float]) -> dict:
    run_id = "run_" + sha256_hex(canonical_json([o["externalId"] for o in observations]))[:12]
    timings: list[dict] = []
    before = canonical_json(world_state_shape(store))
    events_before = len(store.events)

    def timed(step: int, name: str, fn: Callable[[], Any]) -> Any:
        t0 = timer()
        out = fn()
        timings.append({"step": step, "name": name, "ms": timer() - t0})
        return out

    def reconcile_stale() -> None:  # 1. staleness from observation times, never the wall clock
        latest = max((o["at"] for o in observations), default="")
        for rec in store.observations.values():
            rec["stale"] = latest != "" and rec["at"] < _iso_from_ms(_ms(latest) - 30 * 24 * HOUR_MS)

    fresh: list[tuple[dict, str]] = []
    skipped: list[dict] = []

    def classify() -> None:  # 2. unchanged observations (same checksum) are skipped
        for obs in observations:
            checksum = sha256_hex(canonical_json({"externalId": obs["externalId"], "title": obs["title"],
                                                  "text": obs.get("text") or "", "at": obs["at"]}))
            existing = store.observations.get(obs["externalId"])
            (skipped.append(obs) if existing and existing["checksum"] == checksum else fresh.append((obs, checksum)))

    def persist_aggregates() -> None:  # 3. upsert observation entities and mentioned entities
        for obs, checksum in fresh:
            entity_id, entity_ids = upsert(obs)
            store.observations[obs["externalId"]] = {
                "id": "obs_" + sha256_hex(obs["externalId"])[:12], "externalId": obs["externalId"],
                "entityId": entity_id, "checksum": checksum, "at": obs["at"], "entityIds": entity_ids, "stale": False}

    clusters: list[list[dict]] = []

    def group_stories() -> None:  # entity-overlap Jaccard >= 0.5 co-clusters
        records = sorted(store.observations.values(), key=lambda r: r["externalId"])
        used: set[str] = set()
        for seed in records:
            if seed["externalId"] in used:
                continue
            group = [seed]
            used.add(seed["externalId"])
            for other in records:
                if other["externalId"] not in used and jaccard(seed.get("entityIds") or [], other.get("entityIds") or []) >= CLUSTER_JACCARD:
                    group.append(other)
                    used.add(other["externalId"])
            clusters.append(group)

    def score_measure() -> None:  # 4-8. bucketed velocity, milestones, patch source items
        for group in clusters:
            member_ids = sorted(g["externalId"] for g in group)
            story_id = "story_" + sha256_hex(canonical_json(member_ids))[:12]
            times = sorted(_ms(g["at"]) for g in group)
            last = times[-1]

            def velocity(window_ms: float) -> float:
                return sum(1 for t in times if t > last - window_ms) / (window_ms / HOUR_MS)

            prev_hour = sum(1 for t in times if last - 2 * HOUR_MS < t <= last - HOUR_MS)
            curr_hour = sum(1 for t in times if t > last - HOUR_MS)
            store.scores = [s for s in store.scores if s["storyId"] != story_id]
            store.scores.append({"storyId": story_id, "at": _iso_from_ms(last), "velocity1h": velocity(HOUR_MS),
                                 "velocity24h": velocity(24 * HOUR_MS), "velocity7d": velocity(7 * 24 * HOUR_MS),
                                 "trendDirection": (curr_hour > prev_hour) - (curr_hour < prev_hour)})
            entity_ids = sorted({e for g in group for e in (g.get("entityIds") or [])})
            store.stories[story_id] = {
                "id": story_id, "title": group[0]["externalId"], "memberIds": member_ids, "entityNames": list(entity_ids),
                "entityIds": entity_ids, "sources": [], "firstAt": _iso_from_ms(times[0]), "lastAt": _iso_from_ms(last),
                "milestones": [{"kind": "multi-source", "at": _iso_from_ms(times[1])}] if len(group) >= 2 else []}
            for rec in group:
                rec["storyId"] = story_id

    def rebuild_aggregates() -> None:  # 9. rollups on entities
        for entity in store.state["entities"].values():
            mentions = [o for o in store.observations.values() if entity["id"] in (o.get("entityIds") or [])]
            if not mentions:
                continue
            times = sorted(m["at"] for m in mentions)
            entity["aggregates"] = {"mentionCount": len(mentions), "firstSeenAt": times[0], "lastSeenAt": times[-1],
                                    "linkedStoryIds": sorted({m["storyId"] for m in mentions if m.get("storyId")})}

    timed(1, "reconcile-stale", reconcile_stale)
    timed(2, "cluster-classify", classify)
    timed(3, "persist-aggregates", persist_aggregates)
    timed(2, "story-grouping", group_stories)
    timed(4, "score-measure", score_measure)
    timed(9, "rebuild-aggregates", rebuild_aggregates)
    store.persist()
    report = {"runId": run_id, "processed": len(fresh), "skippedUnchanged": len(skipped), "clusters": len(clusters),
              "eventsAppended": len(store.events) - events_before,
              "changed": before != canonical_json(world_state_shape(store)), "stepTimingsMs": timings}
    store.log_audit("ingest", "pipeline", f"run {run_id}: {len(fresh)} processed, {len(skipped)} skipped",
                    run_id=run_id, at=observations[0]["at"] if observations else None)
    return report


# ---------------------------------------------------------------- facade

class WorldModel:
    """The engine facade. Single-writer discipline: writes are serialised by a lock."""

    def __init__(self, namespace: str, storage: str = "memory", directory: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None, auto_merge_at: float = AUTO_MERGE_AT,
                 review_lower: float = REVIEW_LOWER, conflict_policy: str = "most-recent-wins",
                 source_priority: list[str] | None = None):
        if not namespace:
            raise ValueError("WorldModel: namespace is required")
        self.store = EventStore(namespace, storage, directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.auto_merge_at, self.review_lower = auto_merge_at, review_lower
        self.conflict_policy = conflict_policy
        self.source_priority = source_priority or []
        self._lock = threading.RLock()
        self._custom_views: dict[str, dict] = {}

    # ------------------------------------------------------------ writes

    @staticmethod
    def _provenance(source: dict | None) -> dict:
        if not source or not source.get("kind") or source.get("at") is None:
            raise ProvenanceError("World Model: every write carries provenance (kind + at). No provenance, no write.")
        return {**source, "at": iso(source["at"])}

    def upsert_entity(self, type: str, source: dict | None, name: str | None = None,
                      external_key: tuple[str, str] | None = None, attrs: dict | None = None,
                      state: str = "active") -> dict:
        with self._lock:
            src = self._provenance(source)
            name = name or (attrs or {}).get("name")
            if external_key:
                id_ = external_id(type, *external_key)
            elif name:
                id_ = entity_id_from_name(type, name)
            else:
                raise ValueError("World Model: name-keyed entities need a name (or an external_key)")
            if not external_key and name:  # banded resolution against same-type entities
                for other in list(self.store.state["entities"].values()):
                    if other["type"] != type or other["id"] == id_ or not other.get("name"):
                        continue
                    similarity = name_similarity(name, other["name"])
                    verdict = decide(similarity, self.auto_merge_at, self.review_lower)
                    if verdict == "auto-merge":
                        self.store.append("alias", {"aliasId": id_, "primaryId": other["id"], "mergedAt": src["at"],
                                                    "mergedBy": "resolver", "reason": f"similarity {similarity:.3f}"}, src["at"])
                        self.store.log_audit("auto-merge", "resolver", f"{name} -> {other['name']} ({similarity:.3f})", at=src["at"])
                        return self._fold_entity(other["id"], type, src, name, attrs, state)
                    if verdict == "review" and not any(
                            {p["leftId"], p["rightId"]} == {id_, other["id"]} for p in self.store.merge_queue):
                        self.store.merge_queue.append({
                            "id": f"merge_{len(self.store.merge_queue) + 1}", "leftId": id_, "rightId": other["id"],
                            "leftName": name, "rightName": other["name"], "similarity": similarity,
                            "reason": "name similarity in review band [0.82, 0.85)", "status": "pending", "proposedAt": src["at"]})
                        self.store.log_audit("merge-proposed", "resolver",
                                             f"{name} ~ {other['name']} ({similarity:.3f}) queued", at=src["at"])
            return self._fold_entity(id_, type, src, name, attrs, state)

    def _fold_entity(self, id_: str, type: str, src: dict, name: str | None, attrs: dict | None, state: str) -> dict:
        resolved = self.store.resolve_alias(id_)
        entity = {"type": type, "id": resolved, "attrs": dict(attrs or {}), "schemaVersion": 1, "source": src,
                  "createdAt": src["at"], "updatedAt": src["at"], "name": name, "aliases": [name] if name else [],
                  "state": state}
        event = self.store.append("upsertEntity", entity, src["at"])
        self.store.log_audit("upsertEntity", src.get("by") or "system", f"{resolved} ({type})", event["id"], src.get("runId"), src["at"])
        return self.store.state["entities"][resolved]

    def upsert_context(self, type: str, source: dict | None, external_key: tuple[str, str] | None = None,
                       attrs: dict | None = None, parent: str | None = None) -> dict:
        with self._lock:
            src = self._provenance(source)
            id_ = external_id(type, *external_key) if external_key else context_id_from_attrs(type, attrs or {}, parent)
            context = {"type": type, "id": id_, "attrs": dict(attrs or {}), "parent": parent, "schemaVersion": 1,
                       "source": src, "createdAt": src["at"], "updatedAt": src["at"]}
            event = self.store.append("upsertContext", context, src["at"])
            self.store.log_audit("upsertContext", src.get("by") or "system", f"{id_} ({type})", event["id"], src.get("runId"), src["at"])
            return self.store.state["contexts"][id_]

    def relate(self, from_: str, type: str, to: str, source: dict | None, context: str | None = None,
               attrs: dict | None = None, valid_from: str | None = None, valid_to: str | None = None) -> dict:
        with self._lock:
            src = self._provenance(source)
            a, b = self.store.resolve_alias(from_), self.store.resolve_alias(to)
            id_ = relationship_id(a, type, b, context, valid_from)
            if id_ in self.store.state["relationships"]:
                return self.store.state["relationships"][id_]  # content-addressed no-op
            rel = {"id": id_, "from": a, "type": type, "to": b, "context": context, "attrs": attrs,
                   "validFrom": valid_from or src["at"], "validTo": valid_to, "schemaVersion": 1, "source": src,
                   "createdAt": src["at"]}
            event = self.store.append("relate", rel, src["at"])
            self.store.log_audit("relate", src.get("by") or "system", f"{a} -[{type}]-> {b}", event["id"], src.get("runId"), src["at"])
            return self.store.state["relationships"][id_]

    def assert_fact(self, about: str, predicate: str, value: Any, source: dict | None, confidence: float = 0.5,
                    context: str | None = None) -> dict:
        with self._lock:
            src = self._provenance(source)
            subject = self.store.resolve_alias(about)
            id_ = fact_id(subject, predicate, value, src["at"], src["kind"], src.get("url"))
            if id_ in self.store.state["facts"]:
                return self.store.state["facts"][id_]
            fact = {"id": id_, "about": subject, "predicate": predicate, "value": value, "confidence": confidence,
                    "context": context, "source": src, "assertedAt": src["at"]}
            rivals = [f for f in self.store.state["facts"].values()
                      if f["about"] == subject and f["predicate"] == predicate and not f.get("supersededBy")]
            winner = fact
            for rival in rivals:
                if self.conflict_policy == "highest-confidence-wins":
                    rival_wins = rival["confidence"] > winner["confidence"]
                elif self.conflict_policy == "source-priority":
                    rival_wins = self._priority(rival["source"]["kind"]) < self._priority(winner["source"]["kind"])
                else:  # most-recent-wins
                    rival_wins = rival["assertedAt"] > winner["assertedAt"]
                if rival_wins:
                    winner = rival
            event = self.store.append("assertFact", fact, src["at"])
            for f in [fact, *rivals]:
                if f["id"] != winner["id"]:
                    self.store.state["facts"][f["id"]]["supersededBy"] = winner["id"]
            if subject not in self.store.state["entities"]:
                self.store.append("upsertEntity", {
                    "type": subject.split(":")[0] if ":" in subject else "entity", "id": subject, "attrs": {},
                    "schemaVersion": 1, "source": src, "createdAt": src["at"], "updatedAt": src["at"],
                    "aliases": [], "state": "emerging"}, src["at"])
            entity = self.store.state["entities"][subject]
            entity["attrs"][predicate] = winner["value"]
            entity["updatedAt"] = src["at"]
            self.store.log_audit("assertFact", src.get("by") or "system",
                                 f"{subject}.{predicate} = {json.dumps(winner['value'], default=str)} ({self.conflict_policy})",
                                 event["id"], src.get("runId"), src["at"])
            self.store.persist()
            return self.store.state["facts"][id_]

    def _priority(self, kind: str) -> int:
        return self.source_priority.index(kind) if kind in self.source_priority else 1 << 30

    def snapshot(self, at: str | datetime, source: dict | None, entities: list[str], context: str | None = None,
                 attrs: dict | None = None) -> dict:
        """Example A write primitive: record which entities existed at a moment."""
        with self._lock:
            src = self._provenance(source)
            at_iso = iso(at)
            snap = {"id": "snap_" + sha256_hex(canonical_json([at_iso, context, sorted(entities)]))[:16], "at": at_iso,
                    "context": context, "entities": sorted(self.store.resolve_alias(e) for e in entities),
                    "attrs": attrs, "schemaVersion": 1, "source": src}
            event = self.store.append("snapshot", snap, at_iso)
            self.store.log_audit("snapshot", src.get("by") or "system", f"{snap['id']} ({len(entities)} entities)",
                                 event["id"], src.get("runId"), at_iso)
            return snap

    # ------------------------------------------------------------ ingest

    def ingest(self, observations: list[dict]) -> dict:
        with self._lock:
            def upsert(obs: dict) -> tuple[str, list[str]]:
                src = self._provenance({**obs["source"], "at": obs["at"]})
                type_ = obs.get("type") or "item"
                entity = self._fold_entity(external_id(type_, "ingest", obs["externalId"]), type_, src, obs["title"],
                                           {"title": obs["title"], "text": obs.get("text")}, "active")
                ids = []
                for mention in obs.get("entities") or []:
                    mid = entity_id_from_name(mention["type"], mention["name"])
                    self._fold_entity(mid, mention["type"], src, mention["name"], None, "active")
                    target = self.store.resolve_alias(mid)
                    ids.append(target)
                    rid = relationship_id(entity["id"], "mentions", target)
                    if rid not in self.store.state["relationships"]:
                        self.store.append("relate", {"id": rid, "from": entity["id"], "type": "mentions", "to": target,
                                                     "validFrom": src["at"], "schemaVersion": 1, "source": src,
                                                     "createdAt": src["at"]}, src["at"])
                return entity["id"], sorted(ids)

            return run_ingest_pipeline(self.store, observations, upsert, lambda: time.perf_counter() * 1000)

    # ------------------------------------------------------------ reads

    def view(self, name: str) -> Any:
        if name == "graph":
            return GraphView(self.store)
        if name == "facts":
            return FactsView(self.store)
        if name == "snapshot":
            return SnapshotView(self.store)
        if name == "timeline":
            return TimelineView(self.store)
        spec = self._custom_views.get(name)
        if not spec:
            raise KeyError(f'World Model: unknown view "{name}"')
        return _CustomView(self.store, spec)

    def register_view(self, name: str, events: list[str], initial_state: Any, reducer: Callable[[Any, dict], Any]) -> None:
        self._custom_views[name] = {"events": events, "initial": initial_state, "reducer": reducer}

    # ------------------------------------------------------------ operator

    def merge_queue(self) -> list[dict]:
        return self.store.merge_queue

    def approve_merge(self, proposal_id: str, by: str) -> None:
        with self._lock:
            p = next((m for m in self.store.merge_queue if m["id"] == proposal_id and m["status"] == "pending"), None)
            if not p:
                raise KeyError(f"World Model: no pending merge proposal {proposal_id}")
            at = iso(self.clock())
            p.update(status="approved", resolvedAt=at, resolvedBy=by)
            self.store.append("alias", {"aliasId": p["leftId"], "primaryId": p["rightId"], "mergedAt": at,
                                        "mergedBy": by, "reason": p["reason"]}, at)
            self.store.log_audit("merge-approved", by, f"{p['leftName']} -> {p['rightName']}", at=at)

    def reject_merge(self, proposal_id: str, by: str) -> None:
        with self._lock:
            p = next((m for m in self.store.merge_queue if m["id"] == proposal_id and m["status"] == "pending"), None)
            if not p:
                raise KeyError(f"World Model: no pending merge proposal {proposal_id}")
            p.update(status="rejected", resolvedAt=iso(self.clock()), resolvedBy=by)
            self.store.log_audit("merge-rejected", by, f"{p['leftName']} stays separate from {p['rightName']}")

    def audit(self) -> list[dict]:
        return self.store.audit

    def events(self) -> list[dict]:
        return self.store.events

    def state_shape(self) -> dict:
        return world_state_shape(self.store)

    def state_shape_canonical(self) -> str:
        return canonical_json(world_state_shape(self.store))

    def subscribe_events(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        return self.store.subscribe(fn)


# ---------------------------------------------------------------- views

def _valid_at(rel: dict, at: str) -> bool:
    if rel.get("validFrom") and at < rel["validFrom"]:
        return False
    if rel.get("validTo") and at >= rel["validTo"]:
        return False
    return True


class GraphView:
    def __init__(self, store: EventStore):
        self.store = store

    def entity(self, id_: str) -> dict | None:
        return self.store.state["entities"].get(self.store.resolve_alias(id_))

    def neighbors_of(self, id_: str, via: str | None = None, in_context: str | None = None, at: str | None = None) -> list[dict]:
        rid, out = self.store.resolve_alias(id_), []
        for rel in self.store.state["relationships"].values():
            if via and rel["type"] != via:
                continue
            if in_context and rel.get("context") != in_context:
                continue
            if at and not _valid_at(rel, at):
                continue
            if not at and rel.get("validTo"):
                continue  # closed edges are excluded from "now" queries
            other = rel["to"] if rel["from"] == rid else rel["from"] if rel["to"] == rid else None
            if other and other in self.store.state["entities"]:
                out.append(self.store.state["entities"][other])
        return sorted(out, key=lambda e: e["id"])

    def outgoing(self, id_: str, via: str | None = None) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        out = [self.store.state["entities"][r["to"]] for r in self.store.state["relationships"].values()
               if r["from"] == rid and (not via or r["type"] == via) and not r.get("validTo")
               and r["to"] in self.store.state["entities"]]
        return sorted(out, key=lambda e: e["id"])

    def incoming(self, id_: str, via: str | None = None) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        out = [self.store.state["entities"][r["from"]] for r in self.store.state["relationships"].values()
               if r["to"] == rid and (not via or r["type"] == via) and not r.get("validTo")
               and r["from"] in self.store.state["entities"]]
        return sorted(out, key=lambda e: e["id"])

    def entities_in_context(self, context_id: str, type: str | None = None) -> list[dict]:
        seen: dict[str, dict] = {}
        for rel in self.store.state["relationships"].values():
            if rel.get("context") != context_id:
                continue
            for id_ in (rel["from"], rel["to"]):
                e = self.store.state["entities"].get(id_)
                if e and (not type or e["type"] == type):
                    seen[e["id"]] = e
        return [seen[k] for k in sorted(seen)]

    def traverse(self, start_id: str, via: str, direction: str, max_depth: int = 5) -> list[dict]:
        out, visited, frontier = [], set(), [self.store.resolve_alias(start_id)]
        for _ in range(max_depth):
            if not frontier:
                break
            nxt = []
            for id_ in frontier:
                for rel in self.store.state["relationships"].values():
                    if rel["type"] != via or rel.get("validTo"):
                        continue
                    target = (rel["to"] if rel["from"] == id_ else None) if direction == "ancestors" else \
                             (rel["from"] if rel["to"] == id_ else None)
                    if target and target not in visited:
                        visited.add(target)
                        if target in self.store.state["entities"]:
                            out.append(self.store.state["entities"][target])
                        nxt.append(target)
            frontier = nxt
        return out

    def query(self, type: str | None = None, where: Callable[[dict], bool] | None = None) -> list[dict]:
        return sorted((e for e in self.store.state["entities"].values()
                       if (not type or e["type"] == type) and (not where or where(e))), key=lambda e: e["id"])


class FactsView:
    def __init__(self, store: EventStore):
        self.store = store

    def about(self, id_: str) -> list[dict]:
        rid = self.store.resolve_alias(id_)
        return sorted((f for f in self.store.state["facts"].values() if f["about"] == rid), key=lambda f: f["assertedAt"])


class SnapshotView:
    def __init__(self, store: EventStore):
        self.store = store

    def entity_at(self, id_: str, at: str) -> dict | None:
        """Entity state as of a timestamp: replay events with at <= timestamp."""
        stamp = f"{at}T23:59:59.999Z" if len(at) == 10 else at
        state = self.store.fold_until(stamp)
        return state["entities"].get(self.store.resolve_alias(id_, state["aliases"]))


class TimelineView:
    def __init__(self, store: EventStore):
        self.store = store

    def get(self) -> list[dict]:
        return list(self.store.events)

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        return self.store.subscribe(fn)


class _CustomView:
    def __init__(self, store: EventStore, spec: dict):
        self.store, self.spec = store, spec

    def get(self) -> Any:
        state = copy.deepcopy(self.spec["initial"])
        for event in self.store.events:
            if event["kind"] in self.spec["events"]:
                state = self.spec["reducer"](state, event)
        return state


__all__ = ["WorldModel", "ProvenanceError", "normalize_name", "entity_id_from_name", "external_id", "decide",
           "name_similarity", "levenshtein", "jaccard", "canonical_json", "sha256_hex", "iso", "world_state_shape"]
