"""Programmatic entry point: JSON in, JSON out (the hackathon's run contract).

    python run.py run input_examples/example_1.json        # CLI
    POST /api/run   (python -m app.server)                 # HTTP, same body

Request:
    {"run_id": "demo-001",
     "input": {"kind": "part_shortage", "station": "F30", "agv": null, "false_alarm": false,
               "stall_after": null, "seed": 42},
     "options": {"sample_mode": false, "memory": false, "autonomy": "auto"}}   # auto | supervised | advisory

options.sample_mode defaults to the SAMPLE_MODE environment variable.

Response: run_id, status, output (decision, plan, citations, ...), agents, models, trace_id, log_file,
execution_time_seconds. Failures return status "error" with a typed, structured error instead of crashing.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

from app.fixtures import maybe_sample, sample_mode

ROOT = Path(__file__).resolve().parents[1]
AGENT_ROLES = {
    "reflex": "Geofences the incident and stops or re-routes AGVs at once (rules only, under 100 ms)",
    "monitor": "Classifies the alarm as real or sensor noise from thresholds and Bosch vibration evidence",
    "memory": "Recalls H-MEM lessons from past incidents before planning",
    "candidates": "Ranks qualified workers and healthy AGVs from the Meterless World Model (one Zetaris query)",
    "planner": "Nemotron (deep lane) chooses among the candidates and cites the governing clauses",
    "critic": "Checks the plan against OSHA-based safety rules and cites the exact clause for each violation",
    "dryrun": "Simulates the plan for 60 s headless (A* with a reservation table) and checks collisions and ETA",
    "restraint": "Decides ACT, VERIFY, SAFE_HOLD, ESCALATE or DO_NOTHING from confidence and cost",
    "executor": "Dispatches the agents in the simulator",
    "verifier": "Checks that the world changed as predicted and triggers a re-plan if not",
    "safe_mode": "Rule-based fallback when Nightshift is unavailable",
}
KINDS = ("part_shortage", "agv_fault", "overheat", "spill", "robot_cell_fault", "torque_fault", "near_miss", "quality_defect")
SEVERITIES = ("low", "medium", "high", "critical")


class RunError(Exception):
    def __init__(self, type_: str, message: str, recoverable: bool = True):
        super().__init__(message)
        self.type, self.recoverable = type_, recoverable


def _validate(inp: dict) -> dict:
    if inp.get("kind") not in KINDS:
        raise RunError("InvalidInput", f"input.kind must be one of {list(KINDS)}", recoverable=False)
    if inp.get("severity") not in (None, *SEVERITIES):
        raise RunError("InvalidInput", f"input.severity must be one of {list(SEVERITIES)}", recoverable=False)
    if inp["kind"] == "agv_fault" and not inp.get("agv"):
        raise RunError("InvalidInput", "agv_fault needs input.agv, e.g. AGV-3", recoverable=False)
    if inp["kind"] != "agv_fault" and not inp.get("station"):
        raise RunError("InvalidInput", "input.station is required, e.g. F30", recoverable=False)
    return inp


def run_request(req: dict[str, Any], echo: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    run_id = str(req.get("run_id") or f"run-{int(time.time())}")
    trace_id = f"trace-{run_id}"
    log_file = f"logs/{trace_id}.jsonl"
    try:
        inp = _validate(dict(req.get("input") or {}))
        opts = dict(req.get("options") or {})
        # options.sample_mode wins; without it, SAMPLE_MODE=true in the environment means offline too
        sample = bool(opts["sample_mode"]) if "sample_mode" in opts else sample_mode() == "true"
        from app.engines.playbooks import PlaybookStore
        from app.orchestrator import Orchestrator
        from app.sim.grid import Grid
        from app.sim.sim import Sim
        if sample:
            from app.sim.offline import offline_plant
            plant, grid = offline_plant(), Grid.from_generator()
            llm_json = maybe_sample(None, "json", mode="true")
            llm_text = maybe_sample(None, "text", mode="true")
        else:
            from app import runtime
            from app.engines.plant_world import load_plant_world
            try:
                plant = load_plant_world(runtime.run_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")
                grid = Grid.load(runtime.run_sql)
            except Exception as err:  # noqa: BLE001
                raise RunError("UpstreamUnavailable", f"Zetaris query failed ({str(err)[:160]}); "
                               "retry, or set options.sample_mode=true") from err
            llm_json, llm_text = runtime.chat_json, runtime.chat
        sim = Sim.from_plant(plant, grid, seed=int(inp.get("seed", 42)), shift="B")
        agv = f"agv:plant:{inp['agv']}" if inp.get("agv") else None
        if agv and agv not in sim.agents:
            raise RunError("InvalidInput", f"unknown AGV {inp['agv']}", recoverable=False)
        try:
            incident = sim.inject(inp["kind"], plant, station_id=inp.get("station"), agv_id=agv,
                                  false_alarm=bool(inp.get("false_alarm")), severity=inp.get("severity"),
                                  note=inp.get("note"))
        except KeyError as err:
            raise RunError("InvalidInput", f"unknown station {inp.get('station')}", recoverable=False) from err
        memory = PlaybookStore() if opts.get("memory") else None
        mode = opts.get("mode", "dynamic")
        if mode not in ("dynamic", "linear"):
            raise RunError("InvalidInput", "options.mode must be dynamic or linear", recoverable=False)
        common = dict(memory=memory, run_id=run_id, echo=echo, autonomy=opts.get("autonomy", "auto"))
        if mode == "linear":
            orch = Orchestrator(plant, sim, llm_json, llm_text, **common)
        else:
            from app.dynamic import DynamicOrchestrator
            from app.investigate.sources import sql_runner
            orch = DynamicOrchestrator(plant, sim, llm_json, llm_text, **common,
                                       router_llm=None if sample else llm_json,  # sample mode: rule-based router
                                       run_sql=sql_runner("true") if sample else runtime.run_sql)
        stall = (0, int(inp["stall_after"])) if inp.get("stall_after") not in (None, "") else None
        trace = orch.handle(incident, stall=stall)
    except RunError as err:
        return {"run_id": run_id, "status": "error",
                "error": {"type": err.type, "message": str(err), "recoverable": err.recoverable},
                "trace_id": trace_id, "execution_time_seconds": round(time.perf_counter() - started, 2)}
    except Exception as err:  # noqa: BLE001 - never crash silently
        return {"run_id": run_id, "status": "error",
                "error": {"type": type(err).__name__, "message": str(err)[:300], "recoverable": False},
                "trace_id": trace_id, "execution_time_seconds": round(time.perf_counter() - started, 2)}

    steps = trace.steps
    agents: dict[str, dict] = {}
    for st in steps:
        a = agents.setdefault(st.step, {"name": st.step, "role": AGENT_ROLES.get(st.step, ""), "runs": 0, "ok": True})
        a["runs"] += 1
        a["ok"] = a["ok"] and st.ok
    models = sorted({str(st.detail.get("model")) for st in steps if st.detail.get("model")})
    planner = [st for st in steps if st.step in ("planner", "playbook", "verify_plan")
               or (st.step == "refine" and st.detail.get("chosen") == "v2")]
    decision = trace.decision
    plan = trace.plan
    summary = (f"{incident.kind.replace('_', ' ')} at {incident.station_id or incident.zone_id}: "
               f"{decision.decision if decision else 'no decision'}"
               + (f", {plan.rationale}" if plan else ""))
    return {
        "run_id": run_id, "status": "success",
        "output": {
            "summary": summary,
            "incident": incident.model_dump(exclude={"truth_false_alarm"}),
            "decision": decision.decision if decision else None,
            "score": decision.score if decision else None,
            "reasons": decision.reasons if decision else [],
            "plan": plan.model_dump() if plan else None,
            "citations": planner[-1].detail.get("citations", []) if planner else [],
            "critic_rejections": trace.critic_rejections, "replans": trace.replans,
            "safe_mode": trace.safe_mode, "collisions": len(sim.collisions),
            "autonomy": trace.autonomy, "gate": trace.gate, "gate_reasons": trace.gate_reasons,
            "mode": trace.mode, "path": trace.path,
        },
        "agents": list(agents.values()),
        "models": models,
        "trace_id": trace_id, "log_file": log_file,
        "execution_time_seconds": round(time.perf_counter() - started, 2),
    }
