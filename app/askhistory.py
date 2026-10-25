"""Ask-the-floor history: every answer kept with when it was asked, and a re-asked question compared with its last answer.

An answer is a snapshot: the floor keeps logging incidents and the models are not fully deterministic, so the same
question can get a different answer later. Each record carries `asked_at` and, when the question was asked before,
`previous` (that answer's id, time and summary) and `changes` (what moved: the summary, findings that appeared or went
away, and the evidence row counts then and now). Records are appended to a JSONL file and reloaded on start.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

KEEP = 50  # answers held in memory and listed; the file keeps every one


def norm(question: str) -> str:
    """Same question, different spelling: case, punctuation and spacing do not matter."""
    return " ".join(re.sub(r"[^\w\s]", " ", question.lower()).split())


def _rows(result: dict) -> dict[str, int]:
    """Evidence size per source, keyed by kind and task so two runs line up even when the ids differ."""
    out: dict[str, int] = {}
    for e in result.get("evidence") or []:
        n = e.get("rows")
        if n is None and e.get("preview") is not None:
            n = len(e["preview"])
        if n is None and isinstance(e.get("summary"), dict):
            n = e["summary"].get("incidents_matched_in_traces")
        if n is not None:
            key = f"{e.get('type') or 'evidence'}: {e.get('task') or e.get('id')}"
            out[key] = out.get(key, 0) + int(n)
    return out


def compare(before: dict, now: dict) -> dict:
    claims = lambda r: {f.get("claim", "") for f in r.get("findings") or []}  # noqa: E731
    then_rows, now_rows = _rows(before), _rows(now)
    return {"summary_changed": norm(before.get("summary", "")) != norm(now.get("summary", "")),
            "new_findings": sorted(claims(now) - claims(before)),
            "gone_findings": sorted(claims(before) - claims(now)),
            "rows_then": sum(then_rows.values()), "rows_now": sum(now_rows.values()),
            "same_evidence": then_rows == now_rows}


class AskHistory:
    def __init__(self, path: Path | None):
        self.path, self.items, self.lock = path, [], threading.Lock()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-KEEP:]:
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line after a crash

    def add(self, result: dict, now: datetime | None = None) -> dict:
        rec = {**result, "asked_at": (now or datetime.now().astimezone()).isoformat()}
        with self.lock:
            prev = next((r for r in reversed(self.items) if norm(r.get("question", "")) == norm(rec.get("question", ""))), None)
            if prev:
                rec["previous"] = {"ask_id": prev.get("ask_id"), "asked_at": prev.get("asked_at"), "summary": prev.get("summary")}
                rec["changes"] = compare(prev, rec)
            self.items = (self.items + [rec])[-KEEP:]
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
        return rec

    def listing(self) -> list[dict]:
        """Newest first, without the evidence: what the history list shows."""
        with self.lock:
            return [{"ask_id": r.get("ask_id"), "question": r.get("question"), "asked_at": r.get("asked_at"),
                     "summary": r.get("summary"), "findings": len(r.get("findings") or []),
                     "changed": bool((r.get("changes") or {}).get("summary_changed")), "asked_before": "previous" in r}
                    for r in reversed(self.items)]

    def get(self, ask_id: str) -> dict | None:
        with self.lock:
            return next((r for r in self.items if r.get("ask_id") == ask_id), None)

    def last(self) -> dict | None:
        with self.lock:
            return self.items[-1] if self.items else None
