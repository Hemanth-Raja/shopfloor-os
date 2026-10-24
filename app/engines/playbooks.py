"""Incident memory and playbooks, learned from outcomes (H-MEM-style, built without the H-MEM spec).

Every handled incident is captured as a short-term record with provenance: the trace's incident id, the
decision, the critic rules it broke, re-plans and ETAs. Consolidation ("sleep") folds those records into
long-term playbooks keyed by (incident kind, station). A playbook holds:
- the success rate;
- the lessons: critic rules seen with their fixes, e.g. crossing speed 0.5 m/s;
- the pattern that worked: assignee role, AGV kind and speeds.

Each change appends to a trust ledger, which is never rewritten. `recall` returns the playbook as structured
data that the orchestrator feeds to the planner before its first attempt, so known mistakes are not
repeated. That saves model calls and time.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.contracts import Trace, Violation

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "memory" / "playbooks.json"
SHORT_TERM_LIMIT = 50


class PlaybookStore:
    def __init__(self, path: Path | None = DEFAULT_PATH):
        self.path = path
        self.short_term: list[dict] = []
        self.playbooks: dict[str, dict] = {}
        self.ledger: list[dict] = []
        if path and path.exists():
            data = json.loads(path.read_text())
            self.short_term, self.playbooks, self.ledger = data["short_term"], data["playbooks"], data["ledger"]

    @staticmethod
    def key(kind: str, station: str | None) -> str:
        return f"{kind}@{station or '*'}"

    def _log(self, op: str, key: str, detail: str, source: str) -> None:
        self.ledger.append({"seq": len(self.ledger) + 1, "at": datetime.now().astimezone().isoformat(),
                            "op": op, "key": key, "detail": detail, "source": source})

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"short_term": self.short_term, "playbooks": self.playbooks,
                                             "ledger": self.ledger}, indent=1))

    # ---- capture

    def record(self, trace: Trace) -> dict:
        inc = trace.incident
        violations = [v for s in trace.steps if s.step == "critic" for v in s.detail.get("violations", [])]
        verified = [s.ok for s in trace.steps if s.step == "verifier"]
        plan = trace.plan
        rec = {"incident": inc.id, "key": self.key(inc.kind, inc.station_id), "kind": inc.kind,
               "decision": trace.decision.decision if trace.decision else None,
               "success": bool(verified and verified[-1]) or (trace.decision is not None and trace.decision.decision == "VERIFY"),
               "violations": violations, "replans": trace.replans, "critic_rejections": trace.critic_rejections,
               "pattern": {"assignee_types": sorted({a.split(":")[0] for a in plan.assignee_ids}) if plan else [],
                           "agv_routes": [r.model_dump(exclude={"agv_id"}) for r in plan.agv_routes] if plan else []}}
        self.short_term = (self.short_term + [rec])[-SHORT_TERM_LIMIT:]
        self._log("capture", rec["key"], f"{rec['decision']} success={rec['success']}", inc.id)
        self.consolidate()
        self.save()
        return rec

    # ---- consolidate ("sleep")

    def consolidate(self) -> None:
        groups: dict[str, list[dict]] = {}
        for rec in self.short_term:
            groups.setdefault(rec["key"], []).append(rec)
        for key, recs in groups.items():
            lessons: dict[str, dict] = {}
            for rec in recs:
                for v in rec["violations"]:
                    if v["rule_id"] == "GROUNDING":
                        continue
                    entry = lessons.setdefault(v["rule_id"], {**v, "seen": 0, "sources": []})
                    entry["seen"] += 1
                    entry["sources"] = sorted(set(entry["sources"]) | {rec["incident"]})
            good = [r for r in recs if r["success"]]
            book = {"key": key, "kind": recs[-1]["kind"], "outcomes": len(recs), "successes": len(good),
                    "success_rate": round(len(good) / len(recs), 2), "lessons": sorted(lessons.values(), key=lambda l: l["rule_id"]),
                    "working_pattern": good[-1]["pattern"] if good else None,
                    "sources": sorted(r["incident"] for r in recs)}
            if self.playbooks.get(key) != book:
                self._log("consolidate", key, f"{book['outcomes']} outcomes, {len(book['lessons'])} lessons", "sleep")
                self.playbooks[key] = book

    # ---- retrieve

    def recall(self, kind: str, station: str | None) -> dict | None:
        book = self.playbooks.get(self.key(kind, station))
        if book is None:  # fall back to any station for this kind
            same = [b for b in self.playbooks.values() if b["kind"] == kind]
            book = max(same, key=lambda b: b["outcomes"]) if same else None
        if book:
            self._log("retrieve", book["key"], f"{len(book['lessons'])} lessons", "orchestrator")
        return book

    def lessons_as_feedback(self, book: dict | None) -> list[Violation]:
        if not book:
            return []
        return [Violation(rule_id=l["rule_id"], cfr_ref=l["cfr_ref"],
                          detail=f"[learned from {l['seen']} past incident(s)] {l['detail']}") for l in book["lessons"]]
