"""Meterless H-MEM conformance, ported from engines/hmem/conformance/runner.ts.

The vectors in tests/fixtures/hmem_vectors.json are copied unchanged from github.com/Meterless/Meterless
(engines/hmem/conformance/vectors/vectors.json, Apache-2.0). Each vector becomes one pytest case, with the
same id and the same assertions as the TypeScript runner.
"""

import json
from pathlib import Path

import pytest

from app.engines.playbooks import HMEM  # the playbook store runs on this engine

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "hmem_vectors.json").read_text())
T0 = VECTORS["clock"]
DAY = 24 * 60 * 60 * 1000
S = VECTORS["sections"]


def ids(section):
    return [c["id"] for c in S[section]]


@pytest.mark.parametrize("c", S["record-validation"], ids=ids("record-validation"))
def test_record_validation(c):
    hmem = HMEM(clock=lambda: T0)
    try:
        hmem.add(c["input"]["content"], source=c["input"]["source"])
        rejected = False
    except ValueError:
        rejected = True
    assert rejected == (c["expect"] == "reject")


@pytest.mark.parametrize("c", S["feedback-deltas"], ids=ids("feedback-deltas"))
def test_feedback_deltas(c):
    hmem = HMEM(clock=lambda: T0)
    m = hmem.add("feedback target record", confidence=c["start"], source="conformance")
    before = m["accessCount"]
    hmem.feedback(m["id"], c["kind"])
    after = hmem.store.get(m["id"])
    assert after["confidence"] == pytest.approx(c["expectConfidence"], abs=1e-9)
    if "expectAccessDelta" in c:
        assert after["accessCount"] - before == c["expectAccessDelta"]
    if c.get("expectTag"):
        assert c["expectTag"] in after["tags"]


@pytest.mark.parametrize("c", S["ranking-order"], ids=ids("ranking-order"))
def test_ranking_order(c):
    hmem = HMEM(clock=lambda: T0)
    by_key = {r["key"]: hmem.add(r["content"], confidence=r["confidence"], layer=r["layer"], source="conformance")
              for r in c["records"]}
    for r in c["records"]:
        if r.get("supersededByKey"):
            by_key[r["key"]]["supersededBy"] = by_key[r["supersededByKey"]]["id"]
    order = [m["memory"]["id"] for m in hmem.query(c["query"], threshold=0, top_n=10)["memories"]]
    expected = [by_key[k]["id"] for k in c["expectOrder"]]
    for a, b in zip(expected, expected[1:]):
        assert order.index(a) < order.index(b)


@pytest.mark.parametrize("c", S["sleep-guardrails"], ids=ids("sleep-guardrails"))
def test_sleep_guardrails(c):
    now = [T0]
    hmem = HMEM(clock=lambda: now[0])
    by_key = {r["key"]: hmem.add(r["content"], source=r["source"], derived_from=r.get("derivedFrom")) for r in c["records"]}
    now[0] = T0 + c["ageDays"] * DAY
    before = sorted(m["id"] for m in hmem.store.all())
    preview = hmem.sleep.preview()
    assert sorted(m["id"] for m in hmem.store.all()) == before, "preview mutated the store"
    for k in c["expectArchived"]:
        assert by_key[k]["id"] in preview["toArchive"]
    for k in c["expectProtected"]:
        assert by_key[k]["id"] not in preview["toArchive"]
    report = hmem.sleep.execute(preview)
    for k in c["expectProtected"]:
        assert hmem.store.get(by_key[k]["id"]) is not None
    assert isinstance(report["backupId"], str) and report["backupId"]


@pytest.mark.parametrize("c", S["conflict-gate"], ids=ids("conflict-gate"))
def test_conflict_gate(c):
    hmem = HMEM(clock=lambda: T0)
    a = hmem.add(c["a"]["content"], confidence=c["a"]["confidence"], source="conformance")
    b = hmem.add(c["b"]["content"], confidence=c["b"]["confidence"], source="conformance")
    found = hmem.conflicts.scan()
    assert found, "opposing always/never pair on similar content must be detected"
    outcome = hmem.conflicts.auto_resolve(found[0])
    assert outcome in ("resolved", "queued")
    if outcome == "resolved":
        assert any(hmem.store.get(x["id"]).get("supersededBy") for x in (a, b))
    assert hmem.store.get(a["id"]) and hmem.store.get(b["id"]), "conflict resolution must never delete records"


@pytest.mark.parametrize("c", S["ledger-audit"], ids=ids("ledger-audit"))
def test_ledger_audit(c):
    hmem = HMEM(clock=lambda: T0)
    m = hmem.add("audited record", confidence=c["start"], source="conformance")
    hmem.feedback(m["id"], c["kind"])
    history = hmem.ledger.history(m["id"])
    assert [h["action"] for h in history] == c["expectActions"]
    assert history[-1]["previousState"]["confidence"] == pytest.approx(c["expectPrev"])
    assert history[-1]["newState"]["confidence"] == pytest.approx(c["expectNew"])


@pytest.mark.parametrize("c", S["dream-boundary"], ids=ids("dream-boundary"))
def test_dream_boundary(c):
    hmem = HMEM(clock=lambda: T0)
    for r in c["records"]:
        hmem.add(r["content"], type=r["type"], source="conformance")
    count = len(hmem.store.all())
    proposals = hmem.dreaming.dream()
    assert proposals, "two related preferences must produce at least one proposal"
    assert len(hmem.store.all()) == count, "dreaming must not materialize anything before approval"
    created = hmem.dreaming.approve(proposals[0]["id"])
    if created:
        assert created["layer"] == "long_term" and created["derivedFrom"]
