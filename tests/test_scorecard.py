import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.scorecard import calls, decisions


def test_calls_are_counted_per_role_from_the_log(tmp_path, monkeypatch):
    from app import models  # Ultra heads the planner chain but is skipped while Nightshift does not serve it
    monkeypatch.setitem(models.EFFECTIVE, "nvidia/nemotron-3-ultra-550b-a55b", {"ok": False, "answers_as": None})
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    rows = [{"ts": now.isoformat(), "lane": "planner", "requested_model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
             "answering_model": "nvidia/nemotron-3.5-super-vl-120b-a12b", "latency_s": 10, "tokens": {"total": 100}},
            {"ts": now.isoformat(), "lane": "deep", "requested_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
             "answering_model": "nvidia/nemotron-3.5-lightning-30b-a3b", "latency_s": 20, "tokens": {"total": 50}},
            {"ts": (now - timedelta(days=2)).isoformat(), "lane": "router", "requested_model": "x", "answering_model": "x",
             "latency_s": 1}]
    p = tmp_path / "llm.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    c = calls(p, hours=24, now=now)
    assert c["calls"] == 2 and c["tokens"] == 150 and [r["role"] for r in c["roles"]] == ["planner"]
    planner = c["roles"][0]
    assert planner["fallback"] == 1 and planner["models"] == {"nemotron-3.5-super-vl-120b-a12b": 1, "nemotron-3.5-lightning-30b-a3b": 1}
    assert calls(p, hours=None, now=now)["calls"] == 3


def test_decisions_count_who_chose_and_what_nemotron_fixed():
    st = lambda step, ok, **d: SimpleNamespace(step=step, ok=ok, detail=d)  # noqa: E731
    t = SimpleNamespace(path=[{"by": "nemotron (lightning)"}, {"by": "forced"}, {"by": "forced"}, {"by": "rules"}],
                        steps=[st("planner", True, model="nvidia/nemotron-3.5-super"), st("critic", False),
                               st("critic", True, plan_source="nemotron v2")], safe_mode=False)
    d = decisions([t, SimpleNamespace(path=[], steps=[st("planner", True, note="rules plan")], safe_mode=True)])
    assert d["hops"] == {"nemotron": 1, "forced": 2, "rules": 1} and d["critic_rejected"] == 1 and d["fixed_by_nemotron"] == 1
    assert d["plans"] == {"nemotron": 1, "rules or playbook": 1} and d["safe_mode"] == 1


def test_a_rejected_refinement_that_kept_the_checked_plan_is_not_unsafe():
    st = lambda step, ok, **d: SimpleNamespace(step=step, ok=ok, detail=d)  # noqa: E731
    t = SimpleNamespace(path=[], gate="auto_executed", safe_mode=False,
                        steps=[st("critic", True, plan_source="rules v1"), st("critic", False, plan_source="nemotron v2")])
    d = decisions([t])
    assert d["unsafe_executed"] == 0 and d["critic_rejected"] == 1 and d["fixed_by_nemotron"] == 0


def test_anytime_refinements_by_nemotron_are_counted():
    st = lambda step, ok, **d: SimpleNamespace(step=step, ok=ok, detail=d)  # noqa: E731
    t = SimpleNamespace(path=[], gate="auto_executed", safe_mode=False, steps=[
        st("critic", True, plan_source="rules v1"), st("refine", True, chosen="v2", model="nvidia/nemotron", model_call={}),
        st("refine", True, chosen="v1", model="nvidia/nemotron", model_call={}), st("refine", True, chosen="v1")])
    r = decisions([t])["refine"]
    assert (r["nemotron_v2"], r["v2_ran"], r["v1_kept"], r["no_model"]) == (2, 1, 1, 1)


def test_why_v1_was_kept_is_split_so_agreement_is_not_a_loss():
    st = lambda **d: SimpleNamespace(step="refine", ok=True, detail={"model": "nvidia/nemotron", **d})  # noqa: E731
    v1 = {"assignees": ["w:1", "agv:5"], "score": 142.0}
    t = SimpleNamespace(path=[], gate="auto_executed", safe_mode=False, steps=[
        st(chosen="v1", v1=v1, v2={"assignees": ["agv:5", "w:1"], "score": 142.0, "violations": []}),
        st(chosen="v1", v1=v1, v2={"assignees": ["w:1"], "score": None, "violations": ["R-CROSSING"]}),
        st(chosen="v1", v1=v1, v2={"assignees": ["w:2", "agv:5"], "score": 142.0, "violations": []}),
        st(chosen="v1", v1=v1, v2={"assignees": ["w:2", "agv:5"], "score": 150.0, "violations": []})])
    r = decisions([t])["refine"]
    assert r["v1_kept"] == 4 and r["v2_agreed"] == r["v2_rejected"] == r["v2_same_score"] == r["v1_better"] == 1


def test_incidents_count_in_the_period_they_finished_in():
    from app.server import _finished_after
    since = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    assert _finished_after({"finished_at": "2026-10-25T13:30:00+01:00"}, since)
    assert not _finished_after({"finished_at": "2026-10-25T12:30:00+01:00"}, since)
    assert not _finished_after({}, since) and not _finished_after(None, since)
