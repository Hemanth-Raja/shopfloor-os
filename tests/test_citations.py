"""OSHA/SOP clause citations: verbatim excerpts, H-MEM retrieval, critic and planner citations."""

import json
from pathlib import Path

from app.agents.safety_critic import review
from app.contracts import AgvRoute, Plan
from app.engines.citations import CitationIndex, default_index
from app.orchestrator import Orchestrator

from conftest import FakePlanner

SEED = Path(__file__).resolve().parents[1] / "data" / "seed"


def test_osha_excerpts_are_verbatim_ecfr_text():
    ex = {e["ref"]: e for e in json.loads((SEED / "osha_1910_excerpts.json").read_text())}
    assert "slow down and sound the horn at cross aisles" in ex["29 CFR 1910.178(n)(4)"]["text"]
    assert "energy control procedures" in ex["29 CFR 1910.147(c)(1)"]["text"]
    assert "clean and, to the extent feasible, in a dry condition" in ex["29 CFR 1910.22(a)(2)"]["text"]
    assert all(e["source"].startswith("eCFR") for e in ex.values())


def test_rule_citations_put_the_osha_clause_first():
    ix = default_index()
    refs = [c["ref"] for c in ix.for_rule("R-CROSSING", "AGV crosses X-WEST at 1.0 m/s")]
    assert refs == ["29 CFR 1910.178(n)(4)", "SOP-AGV-01 §2"]
    assert ix.for_rule("R-LOTO", "lockout/tagout not valid")[0]["ref"].startswith("29 CFR 1910.147")
    assert all(c["doc"] in ("osha", "sop") for c in ix.for_incident("spill", "coolant spill"))
    assert ix.resolve(["29 CFR 1910.178(n)(4)", "29 CFR 9999.1"]) == [ix.by_ref["29 CFR 1910.178(n)(4)"]]


def test_index_is_h_mem_records_with_sources():
    ix = CitationIndex()
    recs = ix.hmem.store.all()
    assert len(recs) == len(ix.by_ref) and all(r["source"] in ix.by_ref for r in recs)
    assert all(r["layer"] == "long_term" and r["confidence"] == 1.0 for r in recs)


def test_critic_violations_cite_the_exact_clause(sim, plant):
    inc = sim.inject("part_shortage", plant, station_id="F30")
    plan = Plan(incident_id=inc.id, assignee_ids=["agv:plant:AGV-1"], priority="high", rationale="r",
                agv_routes=[AgvRoute(agv_id="agv:plant:AGV-1", to_station="F30", speed_mps=1.0)])
    v = review(plan, inc, plant, sim, default_index()).violations[0]
    assert v.rule_id == "R-CROSSING" and v.citations[0].ref == "29 CFR 1910.178(n)(4)" and v.citations[0].doc == "osha"
    overheat = sim.inject("overheat", plant, station_id="F10", false_alarm=False)
    loto = review(Plan(incident_id=overheat.id, assignee_ids=["worker:plant:W-22"], priority="high", rationale="r"),
                  overheat, plant, sim, default_index()).violations[0]
    assert loto.rule_id == "R-LOTO" and loto.citations[0].ref.startswith("29 CFR 1910.147")


def test_trace_carries_planner_and_critic_citations(sim, plant, tmp_path):
    trace = Orchestrator(plant, sim, FakePlanner(), trace_path=None).handle(sim.inject("part_shortage", plant, station_id="F30"))
    planner = [s for s in trace.steps if s.step == "planner"]
    critic = [s for s in trace.steps if s.step == "critic"]
    assert planner[0].detail["citations"] and planner[0].detail["uncited"] == []
    assert critic[0].detail["violations"][0]["citations"][0]["ref"] == "29 CFR 1910.178(n)(4)"
    assert critic[-1].ok and {c["ref"] for c in critic[-1].detail["citations"]} >= {"29 CFR 1910.178(n)(4)"}
