"""Nightshift receipt signatures: RFC 8032 test vector, a real receipt, tampering."""

import copy
import json
from pathlib import Path

from app.crypto.ed25519 import verify
from app.receipts import verify_all, verify_envelope

FIX = Path(__file__).parent / "fixtures" / "nightshift_receipt.json"


def test_rfc8032_test_vector_1():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
                        "d25bf5f0595bbe24655141438e7a100b")
    assert verify(pub, b"", sig) and not verify(pub, b"x", sig)


def test_a_real_nightshift_receipt_verifies_and_any_edit_breaks_it():
    env = json.loads(FIX.read_text())
    assert verify_envelope(env)
    forged = copy.deepcopy(env)
    forged["body"]["receipt"]["winning_route"]["model"] = "nvidia/some-other-model"
    assert not verify_envelope(forged)


def test_verify_all_counts_only_this_app(tmp_path):
    env = json.loads(FIX.read_text())
    other = copy.deepcopy(env)
    other["body"]["receipt"]["app"] = "someone-else"
    f = tmp_path / "receipts.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in (env, other, env)) + "\n")
    r = verify_all(f, cache_path=None)
    assert r["total"] == 2 and r["verified"] == 2 and r["all_verified"] and len(r["keys"]) == 1
