"""'Powered by' facts for the UI: the last Nightshift call and Nightshift's own signed receipts for this app.

- logs/llm.jsonl is written by app/llm.py for every model call (lane, model that answered, latency).
- Meterless Nightshift writes a signed compute receipt per request to <state dir>/receipts.jsonl, with
  "app": "shopfloor-os" (taken from the meterless-app header that app/llm.py sends). This file is read only,
  never written. Set METERLESS_RECEIPTS to its path; on WSL it is found automatically under
  /mnt/c/Users/*/AppData/Local/ai.meterless.runtime/.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = "shopfloor-os"
ENGINES = ["Meterless World Model", "Meterless H-MEM"]


def _last_line(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - 8192))
        lines = [l for l in fh.read().splitlines() if l.strip()]
    try:
        return json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None


def receipts_path() -> Path | None:
    env = os.environ.get("METERLESS_RECEIPTS")
    if env:
        return Path(env)
    found = sorted(glob.glob("/mnt/c/Users/*/AppData/Local/ai.meterless.runtime/receipts.jsonl"))
    return Path(found[0]) if found else None


def receipts_summary(path: Path | None, app: str = APP) -> dict | None:
    """Count Nightshift's receipts for this app (and its per-role app names) and describe the latest one (no prompts, no secrets)."""
    if path is None or not path.exists():
        return None
    count, last = 0, None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if app not in line:  # cheap pre-filter; the JSON check below is exact
                continue
            try:
                body = json.loads(line)["body"]
                rc = (json.loads(body) if isinstance(body, str) else body)["receipt"]
            except (ValueError, KeyError, TypeError):
                continue
            if not str(rc.get("app") or "").startswith(app):  # shopfloor-os, shopfloor-os-fast, shopfloor-os-code
                continue
            count += 1
            last = rc
    if last is None:
        return {"count": 0}
    usage = last.get("usage") or {}
    return {"count": count, "last_summary": last.get("summary"), "last_receipt_id": last.get("receipt_id"),
            "last_latency_ms": usage.get("latency_ms"), "last_created_at_ms": last.get("created_at"),
            "requested_mode": last.get("requested_mode")}


def powered(sample_mode: bool = False) -> dict:
    last = _last_line(ROOT / "logs" / "llm.jsonl")
    out = {"engines": ENGINES, "provider": "Meterless Nightshift", "sample_mode": sample_mode, "last_call": None,
           "nightshift_receipts": None}
    if last:
        out["last_call"] = {"model": last.get("answering_model"), "lane": last.get("lane"),
                            "latency_s": last.get("latency_s"), "ts": last.get("ts")}
    try:
        out["nightshift_receipts"] = receipts_summary(receipts_path())
    except OSError:
        pass
    return out
