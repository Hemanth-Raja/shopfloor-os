"""Durable, tamper-evident journal of everything the autopilot decides. No model calls.

    append(kind, data) -> event {seq, ts, kind, data, prev, hash}

- Event-sourced: the queue, leases (with fencing tokens), dispatches (with idempotency keys), approvals and results
  are events in one append-only file, fsync'd on every write (logs/journal.jsonl). The file is the source of truth.
- Tamper-evident: every event carries the hash of the previous one. With JOURNAL_HMAC_KEY set in .env the hash is an
  HMAC-SHA256, so an edited or deleted line breaks the chain and `python run.py audit` says where.
- Replicated: a background thread copies new events in batches to Postgres (ops.journal) when the shopfloor-pg
  container is reachable; the file never waits for it.
- Recovery: `recover()` folds the events after a restart. Incidents that were queued or running when the process
  died are reported as interrupted and handed to a person (the floor state they acted on is gone); their leases are
  void, and the fencing-token counter resumes above the last token issued, so a stale holder can never act again.
"""

from __future__ import annotations

import hashlib
import hmac
import fcntl
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / "logs"
GENESIS = "0" * 64


def _key() -> bytes | None:
    k = os.environ.get("JOURNAL_HMAC_KEY")
    return k.encode() if k else None


def digest(prev: str, body: dict, key: bytes | None) -> str:
    msg = (prev + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest() if key else hashlib.sha256(msg).hexdigest()


class Journal:
    def __init__(self, path: Path | None = None, replicate: bool = False):
        self.path = path or LOGS / "journal.jsonl"
        self.lock = threading.Lock()
        self.key = _key()
        self.seq, self.prev = 0, GENESIS
        self.replicated = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line from a crash is ignored (and flagged by verify)
                self.seq, self.prev = ev["seq"], ev["hash"]
        if replicate:
            threading.Thread(target=self._replicate_loop, daemon=True).start()

    def _resync(self, fh) -> None:
        """Another process may have appended since our last write: continue from the file's real last line."""
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        fh.seek(max(0, end - 65536))
        for line in reversed(fh.read().decode("utf-8", "replace").splitlines()):
            try:
                ev = json.loads(line)
                self.seq, self.prev = ev["seq"], ev["hash"]
                return
            except (json.JSONDecodeError, KeyError):
                continue

    def append(self, kind: str, data: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.path.open("a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # two servers (or the CLI) on one journal must not fork the chain
            self._resync(fh)
            body = {"seq": self.seq + 1, "ts": datetime.now().astimezone().isoformat(), "kind": kind, "data": data}
            ev = {**body, "prev": self.prev, "hash": digest(self.prev, body, self.key),
                  "signed": self.key is not None}
            fh.write((json.dumps(ev, default=str) + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
            self.seq, self.prev = ev["seq"], ev["hash"]
            return ev

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"torn": line[:80]})
        return out

    # ---------------------------------------------------------------- integrity

    def verify(self) -> dict:
        """Recompute the chain. -> {ok, events, first_bad, reason}"""
        prev, n = GENESIS, 0
        for ev in self.events():
            n += 1
            if "torn" in ev:
                return {"ok": False, "events": n, "first_bad": n, "reason": "torn line (crash during write?)"}
            body = {k: ev[k] for k in ("seq", "ts", "kind", "data")}
            if ev.get("prev") != prev:
                return {"ok": False, "events": n, "first_bad": ev.get("seq"), "reason": "chain broken (line removed?)"}
            key = self.key if ev.get("signed") else None
            if ev.get("signed") and self.key is None:
                return {"ok": False, "events": n, "first_bad": ev.get("seq"), "reason": "signed, but no key to check"}
            if not hmac.compare_digest(ev.get("hash", ""), digest(prev, body, key)):
                return {"ok": False, "events": n, "first_bad": ev.get("seq"), "reason": "hash mismatch (line edited?)"}
            prev = ev["hash"]
        return {"ok": True, "events": n, "first_bad": None, "reason": None, "signed": self.key is not None}

    # ---------------------------------------------------------------- recovery

    def recover(self) -> dict:
        """Fold the events of the last run. -> open incidents, void leases, last token, pending approvals."""
        open_inc: dict[str, dict] = {}
        leases: dict[str, dict] = {}
        last_token, shift = 0, None
        for ev in self.events():
            k, d = ev.get("kind"), ev.get("data") or {}
            if k == "shift_started":
                shift, open_inc, leases = d, {}, {}
            elif k == "queued":
                open_inc[d["incident"]] = {**d, "state": "queued"}
            elif k == "started" and d.get("incident") in open_inc:
                open_inc[d["incident"]]["state"] = "running"
            elif k == "finished":
                open_inc.pop(d.get("incident"), None)
            elif k == "lease":
                leases[d["resource"]] = d
                last_token = max(last_token, int(d.get("token") or 0))
            elif k in ("release", "preempt"):
                for r in d.get("resources", [d.get("resource")]):
                    leases.pop(r, None)
            elif k == "recovered":
                open_inc, leases = {}, {}
                last_token = max(last_token, int(d.get("last_token") or 0))
            elif k == "shift_finished":
                shift = None
        return {"interrupted": list(open_inc.values()), "void_leases": list(leases.values()),
                "last_token": last_token, "shift": shift}

    # ---------------------------------------------------------------- replication to Postgres

    def _replicate_loop(self, every_s: float = 5.0) -> None:
        while True:
            time.sleep(every_s)
            try:
                self.replicate_once()
            except Exception:  # noqa: BLE001 - the file is the source of truth; try again later
                pass

    def replicate_once(self, container: str = "shopfloor-pg") -> int:
        evs = [e for e in self.events() if "torn" not in e and e["seq"] > self.replicated]
        if not evs:
            return 0
        lit = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731
        rows = ",".join(f"({e['seq']},{lit(e['ts'])},{lit(e['kind'])},{lit(json.dumps(e['data'], default=str))}::jsonb,"
                        f"{lit(e['prev'])},{lit(e['hash'])})" for e in evs[:500])
        sql = ("CREATE TABLE IF NOT EXISTS journal (seq bigint PRIMARY KEY, ts timestamptz, kind text, data jsonb, "
               "prev text, hash text);\n"
               f"INSERT INTO journal (seq, ts, kind, data, prev, hash) VALUES {rows} ON CONFLICT (seq) DO NOTHING;")
        out = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", "ops", "-v",
                              "ON_ERROR_STOP=1", "-q"], input=sql, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:200])
        self.replicated = evs[:500][-1]["seq"]
        return len(evs[:500])
