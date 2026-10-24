"""Verify Meterless Nightshift's signed compute receipts for this app. Read only; no model calls.

Each line of Nightshift's receipts.jsonl is a signed envelope {body, sig_b64, pubkey_b64}. The daemon signs the body
(compact UTF-8 JSON, keys sorted) with Ed25519; the signing key never leaves it. We check every receipt whose app is
shopfloor-os or one of its per-role names (shopfloor-os-fast, -code) against the public key it carries, and report
how many verify and which key(s) signed them (a key change would show as a second fingerprint). Results are cached
by signature in data/cache/receipts_verified.json, so only new receipts cost time.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from app.crypto.ed25519 import verify

CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "receipts_verified.json"
APP = "shopfloor-os"


def signed_bytes(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify_envelope(env: dict) -> bool:
    try:
        pub, sig = base64.b64decode(env["pubkey_b64"]), base64.b64decode(env["sig_b64"])
        body = env["body"] if isinstance(env["body"], dict) else json.loads(env["body"])
        return verify(pub, signed_bytes(body), sig)
    except (KeyError, ValueError, TypeError):
        return False


def verify_all(path: Path | None = None, app: str = APP, cache_path: Path | None = CACHE) -> dict:
    from app.powered import receipts_path
    path = path or receipts_path()
    out = {"app": app, "total": 0, "verified": 0, "failed": [], "keys": {}, "file": str(path) if path else None}
    if path is None or not Path(path).exists():
        out["note"] = "receipts file not found (Nightshift not on this machine?)"
        return out
    cache = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except ValueError:
            cache = {}
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if app not in line:
                continue
            try:
                env = json.loads(line)
                body = env["body"] if isinstance(env["body"], dict) else json.loads(env["body"])
                rc = body["receipt"]
            except (ValueError, KeyError, TypeError):
                continue
            if not str(rc.get("app") or "").startswith(app):
                continue
            out["total"] += 1
            sig = env.get("sig_b64", "")
            ok = cache.get(sig)
            if ok is None:
                ok = cache[sig] = verify_envelope(env)
            if ok:
                out["verified"] += 1
                fp = hashlib.sha256(base64.b64decode(env["pubkey_b64"])).hexdigest()[:16]
                out["keys"][fp] = out["keys"].get(fp, 0) + 1
            else:
                out["failed"].append(rc.get("receipt_id"))
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    out["all_verified"] = out["total"] > 0 and out["verified"] == out["total"]
    return out
