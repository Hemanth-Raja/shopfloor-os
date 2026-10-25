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
import os
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


def incident(kind, station, agv, seed, false_alarm, stall_after, use_memory=True, offline=False, severity=None, note=None,
             linear=False):
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
    inc = sim.inject(kind, plant, station_id=station, agv_id=agv_id, false_alarm=false_alarm, severity=severity, note=note)
    from app.engines.playbooks import PlaybookStore
    memory = PlaybookStore() if use_memory else None
    if linear:
        orch = Orchestrator(plant, sim, chat_json, chat_text, memory=memory, run_id=f"{inc.id}")
    else:
        from app.dynamic import DynamicOrchestrator
        from app.investigate.sources import sql_runner
        replay = sample_mode() == "true"
        orch = DynamicOrchestrator(plant, sim, chat_json, chat_text, memory=memory, run_id=f"{inc.id}",
                                   router_llm=None if replay else chat_json,
                                   run_sql=sql_runner("true") if offline else guarded_sql)
    trace = orch.handle(inc, stall=(0, stall_after) if stall_after is not None else None)
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
                "memory": lambda: f"playbook {d['playbook']} lessons {d['lessons']} success {d['success_rate']}",
                "investigator": lambda: d.get("summary", ""),
                "playbook": lambda: f"{d['playbook']} -> {d['assignees']}",
                "verify_plan": lambda: f"check first: {d['assignees']}",
                "gate": lambda: f"{d['gate']} {d['reasons']}",
                "router": lambda: f"REJECTED {d.get('rejected')} -> {d.get('fallback')}" if d.get("rejected") else d.get("note", ""),
                }.get(st.step, lambda: "")()
        route = d.get("route") or {}
        hop = f"#{d['hop']:<2} " if "hop" in d else ""
        print(f"  {'ok ' if st.ok else 'XX '} {hop}{st.step:<12} {st.ms:>8.1f} ms  {note}")
        if route and st.step != "reflex":
            print(f"        chosen by {route['by']}: {route['reason']}")
    print(f"DECISION {trace.decision.decision if trace.decision else None} | critic rejections {trace.critic_rejections}"
          f" | replans {trace.replans} | safe mode {trace.safe_mode} | collisions {len(sim.collisions)} | {trace.mode}")
    if trace.path:
        print("PATH " + " -> ".join(p["agent"] for p in trace.path))


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


def shift_handover(shifts: int, as_json: bool):
    from app.handover import handover
    from app.runtime import chat

    def show(phase, detail):
        if not as_json:
            print(f"  {phase:<12} {detail}", flush=True)
    out = handover(chat, shifts=shifts, on_phase=show)
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return
    t = out["tokens"]
    print(f"\n{out['note']}\n")
    print(f"{out['events']} journal events in {out['chunks']} chunks | prompt tokens {t['markovian_prompt_tokens']} "
          f"vs {t['standard_prompt_tokens']} re-sending history ({t['saved_percent']}% saved, chars/4 estimate) | "
          f"run {out['run_id']} in logs/markovian.jsonl")


def evaluate(live: bool):
    os.environ.setdefault("MODEL_CONCURRENCY_MAX", "2")  # free endpoints: live runs stay at concurrency 2
    from app.lab.evaluate import run
    llm = None
    if live:
        from app.runtime import chat_json as llm
    res = run(llm)
    p, r, c = res["planted"], res["random"], res["calibration"]
    print(f"planted: {p['unsafe']} unsafe plans, critic recall {p['recall']:.0%}, false rejects {p['false_rejects']}/{p['safe']}")
    print(f"random: {r['runtime_events']} runtime events, {r['rejected_of_those']} rejected; approved with event {r['approved_with_event']}")
    print(f"monitor: Brier {c['brier_monitor']} vs threshold alarm {c['brier_threshold_alarm']}")
    if live:
        lv = res["live"]
        print(f"live: baseline unsafe {lv['baseline_unsafe_or_ungrounded']}/{lv['answered']}, "
              f"full loop unsafe executed {lv['full_loop_unsafe_executed']}, fell back {lv['fell_back']}")
    print("report: docs/eval.md")


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


def shift(minutes: float, seed: int, workers: int, autonomy: str, offline: bool, as_json: bool, rate: float = 2.0):
    from datetime import date

    from app.autonomy.shift import Shift
    from app.dynamic import DynamicOrchestrator
    from app.engines.playbooks import PlaybookStore
    from app.fixtures import maybe_sample, sample_mode
    from app.investigate.sources import sql_runner
    from app.runtime import chat as guarded_chat, chat_json as guarded_json, run_sql as guarded_sql
    from app.sim.grid import Grid
    from app.sim.offline import offline_plant
    from app.sim.sim import Sim

    replay = sample_mode() == "true"
    offline = offline or replay
    if offline:
        plant, grid = offline_plant(), Grid.from_generator()
    else:
        from app.engines.plant_world import load_plant_world
        plant = load_plant_world(guarded_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")
        grid = Grid.load(guarded_sql)
    sim = Sim.from_plant(plant, grid, seed=seed, shift="B")
    from app.sim.invariants import InvariantMonitor
    sim.monitor = InvariantMonitor(plant)  # independent runtime safety checks after every tick
    chat_json, chat_text = maybe_sample(guarded_json, "json"), maybe_sample(guarded_chat, "text")
    memory = PlaybookStore(path=None) if replay else PlaybookStore()
    runner = sql_runner("true") if offline else guarded_sql

    from app.orchestrator import TRACE_PATH
    shift_id = f"shift-{seed}-{int(__import__('time').time())}"

    def make(inc, risk, leases, lock):
        return DynamicOrchestrator(plant, sim, chat_json, chat_text, memory=memory, run_id=inc.id, autonomy=autonomy,
                                   trace_path=TRACE_PATH.parent / f"trace-{shift_id}-{inc.id}.jsonl",
                                   router_llm=None if replay else chat_json, run_sql=runner, leases=leases, risk=risk,
                                   sim_lock=lock)

    def show(kind, data):
        if as_json:
            return
        if kind == "sentinel" and data["action"] in ("raised", "deduplicated", "correlated", "dismissed"):
            print(f"  t={data['tick']:<4} SENTINEL {data['action']:<12} {data['signal']['type']:<16} "
                  f"{data.get('incident') or ''} {data.get('note') or data.get('reason') or ''}")
        elif kind == "started":
            print(f"  START    {data['incident']} risk {data['risk']}")
        elif kind == "finished":
            print(f"  DONE     {data['incident']} {data['kind']:<16} {data['decision']:<9} {data['gate']:<18} "
                  f"route {' > '.join(data['route'])}")
        elif kind == "error":
            print(f"  ERROR    {data}")

    from app.journal import Journal
    sh = Shift(plant, sim, make, minutes=minutes, seed=seed, workers=workers, threads=not replay, journal=Journal(),
               sentinel_llm=None if replay else chat_json, on_event=show, memory=memory, rate_per_min=rate)
    print(f"Shift: {minutes} min, seed {seed}, {len(sh.signals)} signals, {workers} concurrent incidents, "
          f"autonomy {autonomy}, {'replay' if replay else 'offline plant' if offline else 'live'}")
    result = sh.run_headless()
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"\nSUMMARY {result['incidents']} incidents from {result['signals']} signals "
          f"(sentinel {result['sentinel']}); decisions {result['decisions']}")
    print(f"  auto-resolved {result['auto_resolved']}, waiting for a person {len(result['waiting_for_a_person'])}, "
          f"escalated {len(result['escalations'])}; mean {result['mean_minutes_to_resolve']} min to resolve; "
          f"~{result['cars_saved_estimate']} cars saved (95% CI {result['cars_saved_ci95'][0]}-{result['cars_saved_ci95'][1]}); "
          f"{result['preemptions']} pre-emptions; {result['quality_holds']} quality holds; "
          f"max {result['max_concurrent']} at once; {result['wall_seconds']} s")
    print(f"  runtime invariants: {sim.monitor.summary()['violations']} violations {sim.monitor.summary()['by_rule']}")


def receipts(check: bool):
    from app.receipts import verify_all
    r = verify_all()
    if r.get("note"):
        raise SystemExit(r["note"])
    print(f"{r['verified']}/{r['total']} Nightshift receipts for {r['app']}* verify (Ed25519), "
          f"signing key(s): {r['keys']}")
    if r["failed"]:
        print(f"FAILED: {r['failed'][:10]}")
    raise SystemExit(0 if r["all_verified"] else 1)


def audit():
    """Verify the hash chains of the journal and of the approvals log (HMAC with JOURNAL_HMAC_KEY if set)."""
    from app.journal import LOGS, Journal
    bad = 0
    for name in ("journal.jsonl", "approvals.jsonl"):
        path = LOGS / name
        if not path.exists():
            print(f"{name}: no file yet")
            continue
        v = Journal(path).verify()
        bad += not v["ok"]
        print(f"{name}: {v['events']} events, " + ("chain intact" if v["ok"] else
              f"BROKEN at event {v['first_bad']}: {v['reason']}") + (", HMAC-signed" if v.get("signed") else ""))
    rec = Journal().recover()
    if rec["interrupted"]:
        print(f"interrupted incidents from the last run: {[i['incident'] for i in rec['interrupted']]}")
    raise SystemExit(1 if bad else 0)


def watch():
    from app.watch import follow

    try:
        follow()
    except KeyboardInterrupt:
        pass


def health():
    from app.runtime import health as check

    print(json.dumps(check(), indent=1))


def models(check: bool = False):
    if check:
        from app import models as registry
        for m, e in registry.self_check().items():
            print(f"{m}: app {e['app']}, " + (f"answered by {e['answers_as']} in {e['latency_s']} s"
                                              + ("  <- PIN DRIFT" if e.get("drift") else "") if e["ok"] else f"FAILED {e['error']}"))
        for role in registry.CFG["roles"]:
            print(f"  {role:<16} effective chain: {[m.split('/')[-1] for m in registry.effective_chain(role)]}")
        return
    for model_id in list_models():
        print(model_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shop-Floor OS")
    sub = parser.add_subparsers(dest="command", required=True)
    ctx = sub.add_parser("context", help="zone context from Zetaris plus a Nemotron summary")
    ctx.add_argument("--zone", default="FINAL")
    m_p = sub.add_parser("models", help="list model ids offered by Nightshift")
    m_p.add_argument("--check", action="store_true", help="self-check: who really answers each pinned model")
    sub.add_parser("health", help="probe Zetaris and Nightshift through their circuit breakers")
    ask_p = sub.add_parser("ask", help="ask the investigator team a question about the plant and the agents' history")
    ask_p.add_argument("question")
    ask_p.add_argument("--json", action="store_true", help="print the full JSON result")
    sh_p = sub.add_parser("shift", help="run a shift autonomously: Sentinel + concurrent incidents + leases")
    sh_p.add_argument("--minutes", type=float, default=10)
    sh_p.add_argument("--seed", type=int, default=7)
    sh_p.add_argument("--workers", type=int, default=2, help="incidents handled at once")
    sh_p.add_argument("--autonomy", choices=["auto", "supervised", "advisory"], default="auto")
    sh_p.add_argument("--offline", action="store_true", help="offline plant instead of Zetaris (implied by SAMPLE_MODE=true)")
    sh_p.add_argument("--rate", type=float, default=2.0, help="incidents per sim minute (busy floor: 3+)")
    sh_p.add_argument("--json", action="store_true")
    sub.add_parser("audit", help="verify the hash-chained journal and approvals log")
    rc_p = sub.add_parser("receipts", help="verify Meterless Nightshift's signed receipts for this app (Ed25519)")
    rc_p.add_argument("--verify", action="store_true", default=True)
    sub.add_parser("watch", help="live, human-readable view of the agents' logs (run in a second terminal)")
    rf = sub.add_parser("run", help="JSON in, JSON out: run one request file (see input_examples/)")
    rf.add_argument("file")
    rf.add_argument("--out", default=None, help="also write the JSON result to this file")
    lab_p = sub.add_parser("lab", help="Lab Level 2: hypothesis, seeded replays, bootstrap CI, experiment proposal")
    lab_p.add_argument("--write", action="store_true", help="insert the proposal into ops.experiments")
    lab_p.add_argument("--target", choices=["zetaris", "local"], default="zetaris",
                       help="write through Zetaris (default) or straight to the local Postgres container")
    ev = sub.add_parser("eval", help="evaluation: planted unsafe plans, Monitor calibration (offline) -> docs/eval.md")
    ev.add_argument("--live", action="store_true", help="also the Nemotron-only baseline and 3x stability (Nightshift)")
    sub.add_parser("chaos", help="break Nightshift, models and Zetaris on purpose (fake Nightshift) -> docs/chaos.md")
    sub.add_parser("logsync", help="copy agent steps and model calls from logs/ into Postgres (sf_ops.agent_steps, llm_calls)")
    ho = sub.add_parser("handover", help="shift handover note from the journal (Meterless Markovian engine, Nemotron)")
    ho.add_argument("--shifts", type=int, default=1, help="hand over the last N shifts (more = a longer chain)")
    ho.add_argument("--json", action="store_true")
    inc = sub.add_parser("incident", help="run the full agent loop on one injected incident")
    inc.add_argument("--kind", required=True, choices=list(__import__("app.api", fromlist=["KINDS"]).KINDS))
    inc.add_argument("--station", default=None)
    inc.add_argument("--agv", default=None, help="for agv_fault, e.g. AGV-3")
    inc.add_argument("--seed", type=int, default=42)
    inc.add_argument("--false-alarm", action="store_true", help="overheat: sensor noise, not a real fault")
    inc.add_argument("--stall-after", type=int, default=None, help="stall the first planned AGV after N ticks")
    inc.add_argument("--no-memory", action="store_true", help="ignore learned playbooks (shows the critic loop)")
    inc.add_argument("--severity", choices=["low", "medium", "high", "critical"], default=None)
    inc.add_argument("--note", default=None, help="free text from the person reporting it")
    inc.add_argument("--linear", action="store_true", help="the fixed pipeline instead of the dynamic orchestrator")
    inc.add_argument("--offline", action="store_true", help="offline plant instead of Zetaris (implied by SAMPLE_MODE=true)")
    args = parser.parse_args()
    if args.command == "context":
        context(args.zone)
    elif args.command == "incident":
        incident(args.kind, args.station, args.agv, args.seed, args.false_alarm, args.stall_after, not args.no_memory, args.offline,
                 args.severity, args.note, args.linear)
    elif args.command == "health":
        health()
    elif args.command == "ask":
        ask(args.question, args.json)
    elif args.command == "shift":
        shift(args.minutes, args.seed, args.workers, args.autonomy, args.offline, args.json, args.rate)
    elif args.command == "receipts":
        receipts(args.verify)
    elif args.command == "audit":
        audit()
    elif args.command == "watch":
        watch()
    elif args.command == "run":
        run_file(args.file, args.out)
    elif args.command == "lab":
        lab(args.write, args.target)
    elif args.command == "chaos":
        from app.lab.chaos import run as chaos
        res = chaos()
        for r in res["scenarios"]:
            print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['scenario']:<55} {r.get('decision')} / {r.get('gate')}")
        sh = res["shift"]
        print(f"shift under load: {sh['incidents']} incidents, unfinished {sh['unfinished'] or 'none'}, "
              f"unsafe executed {len(sh['unsafe_executed'])}\nreport: docs/chaos.md")
    elif args.command == "logsync":
        from app.logsync import sync
        print(json.dumps(sync()))
    elif args.command == "handover":
        shift_handover(args.shifts, args.json)
    elif args.command == "eval":
        evaluate(args.live)
    else:
        models(args.check)
