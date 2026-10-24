"""Shop-Floor OS command line.

    python run.py context --zone FINAL # one Zetaris query -> Meterless World Model -> Nemotron summary
    python run.py models               # model ids offered by Nightshift
    python run.py run input_examples/example_1.json   # JSON in, JSON out (programmatic entry point)
    python run.py incident --kind part_shortage --station F30            # full agent loop on one incident
    python run.py incident --kind agv_fault --agv AGV-3 --station F30 --stall-after 2
    python run.py incident --kind overheat --station F10 --false-alarm
    SAMPLE_MODE=true python run.py incident ... --no-memory    # replay recorded replies: no keys, no Zetaris
    python run.py ask "Why does F10 keep stopping?"   # investigator team: SQL over Zetaris + agent logs + critic
"""

import argparse
from pathlib import Path
import json
import re

from dotenv import load_dotenv

from app.engines.shopfloor_world import zone_world
from app.llm import chat, list_models
from app.zetaris import run_sql

load_dotenv()

CONTEXT_SQL = """
SELECT i.incident_id, i.ts, i.kind, i.station_id, i.description, i.severity, i.time_to_clear_s,
       i.downtime_min, i.cars_lost, i.cost_usd, i.outcome, i.worker_id, w.name AS worker_name, w.skills AS worker_skills,
       i.agv_id, a.battery_pct AS agv_battery_pct
FROM sf_ops.incidents i
LEFT JOIN sf_hr.workers w ON w.worker_id = i.worker_id
LEFT JOIN sf_fleet.agvs a ON a.agv_id = i.agv_id
WHERE i.zone_id = '{zone}'
ORDER BY i.ts DESC
LIMIT 5
"""
COLUMNS = ["incident_id", "ts", "kind", "station_id", "description", "severity", "time_to_clear_s",
           "downtime_min", "cars_lost", "cost_usd", "outcome", "worker_id", "worker_name", "worker_skills", "agv_id", "agv_battery_pct"]


def context(zone):
    if not re.fullmatch(r"[A-Za-z0-9-]+", zone):
        raise SystemExit("zone must be a zone id such as FINAL, TRIM or X-EAST")
    world, payload, rows, elapsed = zone_world(zone.upper(), run_sql)
    print(" | ".join(COLUMNS))
    for r in rows[:5]:
        print(" | ".join("" if r.get(c) is None else str(r.get(c)) for c in COLUMNS))
    print(f"{len(rows)} rows from Zetaris in {elapsed:.2f}s (one federated query)")
    counts = ", ".join(f"{n} {t}" for t, n in payload["counts"].items() if n)
    checks = [f"{e['id']}: {e['checks']}" for k in ("incidents", "agvs") for e in payload[k] if e.get("checks")]
    print(f"World model: {counts}; {len(world.events())} events, {len(world.audit())} audit entries")
    for line in checks:
        print(f"  check {line}")

    # The prompt sees world-model state (entities, links, grounded checks), not raw rows.
    reply = chat(
        "deep",
        [
            {"role": "system", "content": "You are a shift assistant in a car assembly plant. Use only the entities "
                                          "and checks in the world-model JSON; do not invent ids. Answer in two sentences."},
            {"role": "user", "content": f"World model for zone {zone.upper()}:\n{json.dumps(payload)}\n\nWhat stands out?"},
        ],
        max_tokens=2000,  # the deep lane reasons before answering; 800 cut replies short
    )
    print(f"\nNemotron ({reply.model}):\n{reply.text}")


def incident(kind, station, agv, seed, false_alarm, stall_after, use_memory=True, offline=False):
    from datetime import date

    from app.engines.plant_world import load_plant_world
    from app.fixtures import maybe_sample, sample_mode
    from app.orchestrator import Orchestrator
    from app.runtime import chat as guarded_chat, chat_json as guarded_json, run_sql as guarded_sql
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim

    offline = offline or sample_mode() == "true"  # replay needs neither Zetaris nor Nightshift
    if offline:
        plant, grid = offline_plant(), Grid.from_generator()
    else:
        plant = load_plant_world(guarded_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")  # one Zetaris query
        grid = Grid.load(guarded_sql)
    sim = Sim.from_plant(plant, grid, seed=seed, shift="B")
    chat_json, chat_text = maybe_sample(guarded_json, "json"), maybe_sample(guarded_chat, "text")
    agv_id = f"agv:plant:{agv}" if agv else None
    inc = sim.inject(kind, plant, station_id=station, agv_id=agv_id, false_alarm=false_alarm)
    from app.engines.playbooks import PlaybookStore
    memory = PlaybookStore() if use_memory else None
    trace = Orchestrator(plant, sim, chat_json, chat_text, memory=memory, run_id=f"{inc.id}").handle(inc, stall=(0, stall_after) if stall_after is not None else None)
    print(f"{inc.id} {inc.kind} at {inc.station_id or inc.zone_id} ({inc.zone_id}) sensors={inc.sensors}")
    for st in trace.steps:
        d = st.detail
        note = {"reflex": lambda: f"fence {d['fence_cells']} cells, stopped {d['stopped']}, rerouted {d['rerouted']}",
                "monitor": lambda: d["summary"],
                "candidates": lambda: f"workers {d['workers']} agvs {d['agvs']}",
                "planner": lambda: f"#{d['attempt']} {d['priority']} -> {d['assignees']} {[(r['agv_id'], r['to_station'], r['speed_mps'], r['crossing_speed_mps']) for r in d['agv_routes']]}",
                "critic": lambda: "ok" if st.ok else "; ".join(f"{v['rule_id']}: {v['detail']}" for v in d["violations"]),
                "dryrun": lambda: f"ok={st.ok} eta_s={d['eta_s']} {d['reasons']}",
                "restraint": lambda: f"{d['decision']} score={d['score']} {d['reasons'][-1]}",
                "executor": lambda: f"eta_s={d['eta_s']}",
                "verifier": lambda: "as predicted" if st.ok else f"MISMATCH {d['mismatches']} -> re-plan",
                "safe_mode": lambda: d.get("error", ""),
                "memory": lambda: f"playbook {d['playbook']} lessons {d['lessons']} success {d['success_rate']}"}.get(st.step, lambda: "")()
        print(f"  {'ok ' if st.ok else 'XX '} {st.step:<10} {st.ms:>8.1f} ms  {note}")
    print(f"DECISION {trace.decision.decision} | critic rejections {trace.critic_rejections} | replans {trace.replans}"
          f" | safe mode {trace.safe_mode} | collisions {len(sim.collisions)}")


def lab(write: bool, target: str = "zetaris"):
    from app.fixtures import maybe_sample
    from app.lab.experiment import experiment_row, run_experiment, write_local, write_zetaris
    from app.runtime import chat_json as guarded_json, run_sql as guarded_sql

    result = run_experiment(guarded_sql, maybe_sample(guarded_json, "json"))
    h = result["hypothesis"]
    print(f"Hypothesis ({result['source']}): {h['hypothesis']}")
    print(f"Policy {h['policy']} | expected {h['expected_effect_pct']}%")
    print(f"{result['incidents']} historical incidents re-scored, {len(result['replays'])} seeded replays (no LLM):")
    for r in result["replays"][:5]:
        print(f"  seed {r['seed']}: baseline {r['baseline_mean_s']} s -> treatment {r['treatment_mean_s']} s")
    print(f"Effect {result['effect_pct']}% (95% CI {result['ci_low_pct']}% to {result['ci_high_pct']}%), "
          f"{result['baseline_mean_s']} s -> {result['treatment_mean_s']} s | {result['status']}")
    row = experiment_row(result)
    if write and target == "zetaris":
        try:
            print(f"Wrote {write_zetaris(row, guarded_sql)} through Zetaris to sf_ops.experiments")
        except Exception as err:  # noqa: BLE001 - keep the proposal: fall back to the local Postgres path
            print(f"Zetaris write failed ({str(err)[:120]}); writing locally instead")
            print(f"Wrote {write_local(row)} to local Postgres ops.experiments")
    elif write:
        print(f"Wrote {write_local(row)} to local Postgres ops.experiments")
    else:
        print("Proposal (not written):", json.dumps(row))


def run_file(path: str, out: str | None):
    from app.api import run_request

    req = json.loads(Path(path).read_text())
    result = run_request(req, echo=True)
    text = json.dumps(result, indent=2, default=str)
    print(text)
    if out:
        Path(out).write_text(text + "\n")


def ask(question: str, as_json: bool = False):
    from app.engines.playbooks import PlaybookStore
    from app.fixtures import maybe_sample, sample_mode
    from app.investigate.investigator import Investigator
    from app.investigate.sources import sql_runner
    from app.runtime import chat_json as guarded_json

    def show(_, st):
        if not as_json:
            print(f"  {'ok ' if st['ok'] else 'XX '} {st['step']:<12} {st['ms']:>9.1f} ms")

    inv = Investigator(sql_runner(sample_mode()), maybe_sample(guarded_json, "json"), on_step=show, memory=PlaybookStore(),
                       sample=sample_mode())
    result = inv.run(question)
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"\nQ: {question}\nA: {result['summary']}")
    for f in result["findings"]:
        print(f"  - {f['claim']} [{', '.join(f['evidence'])}]")
    for f in result["dropped"]:
        print(f"  dropped by the critic: {f['claim']} ({f['reason']})")
    print(f"Recommendation: {result['recommendation']}")
    for e in result["evidence"]:
        if e["type"] == "sql":
            print(f"\n{e['id']} SQL ({'ok, %s rows' % e['rows'] if e.get('ok') else 'failed: ' + str(e.get('error'))}):\n{e.get('sql', '')}")
        else:
            print(f"\n{e['id']} logs: {json.dumps(e['summary'], default=str)[:400]}")
    print(f"\nmodels {result['models']} | trace {result['log_file']} | {result['execution_time_seconds']} s")


def watch():
    from app.watch import follow

    try:
        follow()
    except KeyboardInterrupt:
        pass


def health():
    from app.runtime import health as check

    print(json.dumps(check(), indent=1))


def models():
    for model_id in list_models():
        print(model_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shop-Floor OS")
    sub = parser.add_subparsers(dest="command", required=True)
    ctx = sub.add_parser("context", help="zone context from Zetaris plus a Nemotron summary")
    ctx.add_argument("--zone", default="FINAL")
    sub.add_parser("models", help="list model ids offered by Nightshift")
    sub.add_parser("health", help="probe Zetaris and Nightshift through their circuit breakers")
    ask_p = sub.add_parser("ask", help="ask the investigator team a question about the plant and the agents' history")
    ask_p.add_argument("question")
    ask_p.add_argument("--json", action="store_true", help="print the full JSON result")
    sub.add_parser("watch", help="live, human-readable view of the agents' logs (run in a second terminal)")
    rf = sub.add_parser("run", help="JSON in, JSON out: run one request file (see input_examples/)")
    rf.add_argument("file")
    rf.add_argument("--out", default=None, help="also write the JSON result to this file")
    lab_p = sub.add_parser("lab", help="Lab Level 2: hypothesis, seeded replays, bootstrap CI, experiment proposal")
    lab_p.add_argument("--write", action="store_true", help="insert the proposal into ops.experiments")
    lab_p.add_argument("--target", choices=["zetaris", "local"], default="zetaris",
                       help="write through Zetaris (default) or straight to the local Postgres container")
    inc = sub.add_parser("incident", help="run the full agent loop on one injected incident")
    inc.add_argument("--kind", required=True, choices=["part_shortage", "agv_fault", "overheat", "spill"])
    inc.add_argument("--station", default=None)
    inc.add_argument("--agv", default=None, help="for agv_fault, e.g. AGV-3")
    inc.add_argument("--seed", type=int, default=42)
    inc.add_argument("--false-alarm", action="store_true", help="overheat: sensor noise, not a real fault")
    inc.add_argument("--stall-after", type=int, default=None, help="stall the first planned AGV after N ticks")
    inc.add_argument("--no-memory", action="store_true", help="ignore learned playbooks (shows the critic loop)")
    inc.add_argument("--offline", action="store_true", help="offline plant instead of Zetaris (implied by SAMPLE_MODE=true)")
    args = parser.parse_args()
    if args.command == "context":
        context(args.zone)
    elif args.command == "incident":
        incident(args.kind, args.station, args.agv, args.seed, args.false_alarm, args.stall_after, not args.no_memory, args.offline)
    elif args.command == "health":
        health()
    elif args.command == "ask":
        ask(args.question, args.json)
    elif args.command == "watch":
        watch()
    elif args.command == "run":
        run_file(args.file, args.out)
    elif args.command == "lab":
        lab(args.write, args.target)
    else:
        models()
