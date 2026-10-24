"""The 'Powered by' strip: last Nightshift call from logs and Nightshift's signed receipts for this app."""

import json

from app.powered import receipts_summary


def test_receipts_are_counted_for_this_app_only(tmp_path):
    def receipt(app, rid, latency):
        body = {"receipt": {"app": app, "receipt_id": rid, "summary": f"ok · Nemotron · {rid}",
                            "usage": {"latency_ms": latency}, "created_at": 1, "requested_mode": "meterless/auto"}}
        return json.dumps({"body": json.dumps(body), "pubkey_b64": "x", "sig_b64": "y"})
    path = tmp_path / "receipts.jsonl"
    path.write_text("\n".join([receipt("shopfloor-os", "r1", 100), receipt("other-app", "r2", 5),
                               receipt("shopfloor-os", "r3", 7932), "not json"]) + "\n")
    s = receipts_summary(path)
    assert s["count"] == 2 and s["last_receipt_id"] == "r3" and s["last_latency_ms"] == 7932


def test_missing_receipts_file_is_fine(tmp_path):
    assert receipts_summary(tmp_path / "nope.jsonl") is None and receipts_summary(None) is None
