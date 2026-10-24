"""SAMPLE_MODE: record and replay model replies, so the demo runs without Nightshift or keys.

    SAMPLE_MODE=false   (default) call Nightshift normally
    SAMPLE_MODE=record  call Nightshift and save each reply to logs/fixtures/<hash>.json
    SAMPLE_MODE=true    replay saved replies only, with no network; a missing reply raises LLMUnavailable,
                        so the orchestrator falls back to safe mode instead of failing

The key is a sha256 of (kind, lane, schema, messages), so a replay matches when the scenario and seed match.
Replays use the offline plant (no Zetaris either). Record with the same `--offline` setting you replay with.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from dotenv import load_dotenv

from app.llm import LLMUnavailable

load_dotenv()

FIXTURES = Path(__file__).resolve().parents[1] / "logs" / "fixtures"


def sample_mode() -> str:
    value = os.environ.get("SAMPLE_MODE", "false").strip().lower()
    return {"1": "true", "yes": "true", "replay": "true"}.get(value, value)


def _key(kind: str, lane: str, messages: list[dict], schema=None) -> str:
    blob = json.dumps({"kind": kind, "lane": lane, "schema": getattr(schema, "__name__", None), "messages": messages},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def maybe_sample(fn: Callable, kind: str, mode: str | None = None, folder: Path | None = None) -> Callable:
    """Wrap chat_json (kind="json") or chat (kind="text") according to SAMPLE_MODE."""
    mode = mode or sample_mode()
    folder = folder or FIXTURES
    if mode not in ("record", "true"):
        return fn

    def wrapped(lane, messages, schema=None, **kwargs):
        key = _key(kind, lane, messages, schema)
        path = folder / f"{key}.json"
        if mode == "true":
            if not path.exists():
                raise LLMUnavailable(f"SAMPLE_MODE: no recorded reply {key} for this scenario")
            saved = json.loads(path.read_text())
            if kind == "json":
                return SimpleNamespace(data=schema.model_validate(saved["data"]), text=saved["text"],
                                       model=saved["model"] + " (replay)", latency_s=0.0)
            return SimpleNamespace(text=saved["text"], model=saved["model"] + " (replay)", latency_s=0.0, truncated=False)
        reply = fn(lane, messages, schema, **kwargs) if kind == "json" else fn(lane, messages, **kwargs)
        folder.mkdir(parents=True, exist_ok=True)
        record = {"kind": kind, "lane": lane, "model": reply.model, "text": reply.text,
                  "data": reply.data.model_dump() if kind == "json" else None}
        path.write_text(json.dumps(record, indent=1))
        return reply

    return wrapped
