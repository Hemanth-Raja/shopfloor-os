"""Meterless Markovian engine (headless port) and the shift handover built on it. No network: stub generators."""

from types import SimpleNamespace

from app.engines import markovian as mk
from app.handover import handover, last_shift, slices


def echo_gen(final_at=None):
    calls = []

    def gen(prompt, system):
        calls.append(prompt)
        n = len(calls)
        end = mk.FINAL if final_at == n else f"{mk.STATE} Status: processed {n} slices, open SIM-{n}"
        return f"Notes for chunk {n}. Key insight: slice {n} looked fine.\n{end}"
    return gen, calls


def test_config_clamps_to_spec_bounds_and_projects_the_curve():
    c = mk.ChunkConfig(chunkSize=50, carryoverTokens=99999, maxChunks=0).clamped()
    assert (c.chunkSize, c.carryoverTokens, c.maxChunks) == (1000, 32768, 1)
    p = mk.ChunkConfig().projected(3)
    assert p[2] == {"step": 3, "standard": 24000, "markovian": 8256}


def test_markers_are_stripped_and_state_is_carried_without_the_history():
    gen, calls = echo_gen()
    eng = mk.MarkovianEngine(gen, log_path=None)
    run = eng.run("summarise", ["a" * 4000, "b" * 4000, "c" * 4000])
    assert run.completed and len(run.chunks) == 3
    assert all(mk.STATE not in c.content for c in run.chunks)
    assert "processed 1 slices" in calls[1] and "a" * 100 not in calls[1]  # carryover, not the earlier input
    assert "processed 2 slices" in run.chunks[2].carryover


def test_final_marker_stops_the_chain_and_reflection_failure_keeps_the_output():
    gen, _ = echo_gen(final_at=2)
    def boom(p, s):
        raise RuntimeError("model down")
    run = mk.MarkovianEngine(gen, reflector=boom, log_path=None).run("g", ["x", "y", "z"])
    assert run.completed and len(run.chunks) == 2 and run.reflection is None and run.content


def test_cascade_order_explicit_llm_heuristic_truncate():
    assert mk.compress("", "text", "Status: three incidents open", 128)[1] == "explicit"
    assert mk.compress("", "text", None, 128, lambda p, s: "1) SIM-1 open 2) AGV-3 busy")[1] == "llm"
    bad = lambda p, s: (_ for _ in ()).throw(TimeoutError())  # noqa: E731
    assert mk.compress("", "Therefore: AGV-3 needs service soon", None, 128, bad)[1] == "heuristic"
    out, tier = mk.compress("", "word " * 2000, None, 128)
    assert tier == "truncate" and len(out) <= 128 * 4


def test_token_math_matches_the_spec_and_abort_keeps_partial_output():
    run = mk.MarkovianRun(id="r", timestamp=0, prompt="", mode="ARCHITECT",
                          chunks=[mk.ChunkInfo(i, 100, "", "") for i in (1, 2, 3)])
    mk.account(run, 10)
    assert (run.standardTokens, run.markovianTokens, run.totalTokensSaved) == (600, 330, 270)
    gen, _ = echo_gen()
    stop = iter([False, False, True, True])
    run = mk.MarkovianEngine(gen, log_path=None).run("g", ["x", "y", "z"], abort=lambda: next(stop))
    assert run.aborted and len(run.chunks) == 1 and not run.completed


def test_handover_reads_the_last_shift_in_bounded_slices(tmp_path):
    events = [{"seq": 0, "kind": "shift_started", "data": {}}]
    events += [{"seq": i, "kind": "queued", "data": {"incident": f"SIM-1-{i:03d}", "kind": "spill"}} for i in range(1, 120)]
    events += [{"seq": 200 + i, "kind": "lease", "data": {"resource": "agv:plant:AGV-2"}} for i in range(5)]
    events += [{"seq": 300, "kind": "shift_finished", "data": {"incidents": 119}}]
    old = [{"seq": -1, "kind": "shift_started", "data": {}}, {"seq": -2, "kind": "shift_finished", "data": {}}]
    assert last_shift(old + events)[0]["seq"] == 0
    assert len(slices(last_shift(events))) == 4 and "AGV-2=5" in slices(events)[-1]
    seen = []

    def chat(lane, messages, role=None):
        seen.append(role)
        n = seen.count("handover")
        return SimpleNamespace(text="Handover: 119 spills." if role == "handover_reflect"
                               else f"ok {n} {mk.STATE} Status: {n} slices read, 119 incidents queued")
    out = handover(chat, events=old + events, log_path=tmp_path / "m.jsonl")
    assert out["completed"] and out["chunks"] == 4 and out["note"] == "Handover: 119 spills."
    t = out["tokens"]
    assert t["markovian_prompt_tokens"] < t["standard_prompt_tokens"] and t["per_step"][0]["carryover"] == 0
    assert (tmp_path / "m.jsonl").exists()
