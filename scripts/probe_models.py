"""Probe the models Nightshift offers: does each one answer itself, or silently fall back to another? Read-only.

    python scripts/probe_models.py            # NVIDIA chat models only (remote; nothing is downloaded or loaded)

Writes data/model_probe.json: requested model, answering model, latency, whether the reply was usable. Local-runtime
models and meterless/* aliases are listed but not called: calling them can make Nightshift load or download a model.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm import get_client  # noqa: E402 - the same client and headers as every other call

OUT = Path(__file__).resolve().parents[1] / "data" / "model_probe.json"
SKIP = ("embed",)


def main() -> None:
    client = get_client(timeout_s=120)
    ids = [m.id for m in client.models.list()]
    results = []
    for mid in ids:
        if not mid.startswith("nvidia/") or any(s in mid for s in SKIP):
            continue
        started = time.perf_counter()
        try:
            r = client.chat.completions.create(model=mid, max_tokens=400, timeout=120, messages=[
                {"role": "user", "content": "Reply with exactly one word: OK"}])
            text = (r.choices[0].message.content or "").strip() if r.choices else ""
            rec = {"requested": mid, "answering": r.model or mid, "latency_s": round(time.perf_counter() - started, 2),
                   "reply": text[:40], "ok": bool(text)}
        except Exception as err:  # noqa: BLE001 - record and move on
            rec = {"requested": mid, "answering": None, "latency_s": round(time.perf_counter() - started, 2),
                   "error": f"{type(err).__name__}: {str(err)[:160]}", "ok": False}
        rec["falls_back"] = bool(rec.get("answering")) and rec["answering"] != mid
        print(json.dumps(rec), flush=True)
        results.append(rec)
    OUT.write_text(json.dumps({"probed_at": datetime.now(timezone.utc).isoformat(), "results": results,
                               "not_probed": [m for m in ids if not m.startswith("nvidia/") or any(s in m for s in SKIP)]},
                              indent=1) + "\n")


if __name__ == "__main__":
    main()
