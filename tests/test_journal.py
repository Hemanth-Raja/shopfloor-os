"""Durable journal: hash chain and HMAC, tamper detection, crash recovery, fencing tokens, idempotent dispatch."""

import json

import pytest

from app.autonomy.leases import LeaseBook
from app.connectors import SimFleet, StaleToken
from app.journal import Journal


def test_chain_detects_edits_deletions_and_torn_lines(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    for i in range(4):
        j.append("queued", {"incident": f"I{i}", "risk": i})
    assert j.verify()["ok"]
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    edited = json.loads(lines[1]); edited["data"]["risk"] = 99
    (tmp_path / "e.jsonl").write_text("\n".join([lines[0], json.dumps(edited)] + lines[2:]) + "\n")
    assert Journal(tmp_path / "e.jsonl").verify()["reason"].startswith("hash mismatch")
    (tmp_path / "d.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert Journal(tmp_path / "d.jsonl").verify()["reason"].startswith("chain broken")
    (tmp_path / "t.jsonl").write_text("\n".join(lines) + '\n{"seq": 5, "ts"')
    assert Journal(tmp_path / "t.jsonl").verify()["reason"].startswith("torn")


def test_hmac_signed_chain_needs_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_HMAC_KEY", "k" * 32)
    j = Journal(tmp_path / "j.jsonl")
    j.append("approval", {"incident": "I1", "approved": True})
    assert j.verify()["ok"] and j.verify()["signed"]
    monkeypatch.setenv("JOURNAL_HMAC_KEY", "another-key")
    assert not Journal(tmp_path / "j.jsonl").verify()["ok"]  # forging needs the key


def test_recovery_hands_interrupted_incidents_to_a_person_and_tokens_move_on(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append("shift_started", {"minutes": 5})
    j.append("queued", {"incident": "A", "risk": 30})
    j.append("queued", {"incident": "B", "risk": 120})
    j.append("started", {"incident": "B"})
    j.append("lease", {"resource": "agv:plant:AGV-5", "incident": "B", "token": 7})
    j.append("queued", {"incident": "C", "risk": 10})
    j.append("finished", {"incident": "C"})
    # --- the process dies here; a new one starts and reads the file
    rec = Journal(tmp_path / "j.jsonl").recover()
    assert {i["incident"]: i["state"] for i in rec["interrupted"]} == {"A": "queued", "B": "running"}
    assert [l["resource"] for l in rec["void_leases"]] == ["agv:plant:AGV-5"] and rec["last_token"] == 7
    book = LeaseBook()
    book.next_token = rec["last_token"] + 1
    assert book.acquire("D", 50, ["agv:plant:AGV-5"])["tokens"]["agv:plant:AGV-5"] == 8


def test_fencing_refuses_a_preempted_holder_and_dispatch_is_idempotent(plant, sim):
    book, fleet = LeaseBook(), SimFleet(sim)
    agv = "agv:plant:AGV-5"
    low = book.acquire("LOW", 20, [agv])["tokens"][agv]
    high = book.acquire("HIGH", 130, [agv])["tokens"][agv]  # pre-empts LOW
    assert high > low
    first = fleet.dispatch(agv, (20, 13), token=high, key="HIGH:r1:agv")
    assert fleet.dispatch(agv, (20, 13), token=high, key="HIGH:r1:agv") is first  # retried command: no second move
    with pytest.raises(StaleToken):
        fleet.dispatch(agv, (30, 13), token=low, key="LOW:r1:agv")  # the loser cannot move it any more
    assert fleet.refused[0]["token"] == low


def test_a_shift_journals_queue_leases_and_results(tmp_path, plant, grid):
    from app.autonomy.shift import Shift
    from app.dynamic import DynamicOrchestrator
    from app.sim.sim import Sim
    from tests.conftest import FakePlanner
    sim = Sim.from_plant(plant, grid, seed=5, shift="B")
    j = Journal(tmp_path / "j.jsonl")

    def make(i, risk, leases, lock):
        return DynamicOrchestrator(plant, sim, FakePlanner(), trace_path=None, leases=leases, risk=risk, sim_lock=lock,
                                   run_sql=lambda sql: [[3, 1, 2.0, 900.0]])
    sh = Shift(plant, sim, make, minutes=3, seed=5, workers=2, rate_per_min=3, journal=j)
    sh.sentinel.log_path = None
    out = sh.run_headless()
    kinds = [e["kind"] for e in j.events()]
    assert kinds[0] == "shift_started" and kinds[-1] == "shift_finished" and "lease" in kinds
    assert kinds.count("finished") == out["incidents"] and j.verify()["ok"]
    assert Journal(tmp_path / "j.jsonl").recover()["interrupted"] == []


def test_two_writers_on_one_journal_keep_one_chain(tmp_path):
    a, b = Journal(tmp_path / "j.jsonl"), Journal(tmp_path / "j.jsonl")  # e.g. a live and a replay server
    for i in range(5):
        a.append("x", {"i": i})
        b.append("y", {"i": i})
    v = Journal(tmp_path / "j.jsonl").verify()
    assert v["ok"] and v["events"] == 10
