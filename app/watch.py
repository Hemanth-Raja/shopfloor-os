"""Live, human-readable view of what the agents are doing: follows the logs as they are written.

    python run.py watch          # run it in a second terminal while the UI or a CLI run is going

It follows every logs/trace-<run_id>.jsonl (each agent step), logs/llm.jsonl (each Nemotron call) and
logs/approvals.jsonl (each human judgement), and prints one plain-English line per event.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / "logs"
COLOURS = {"ok": "\033[32m", "bad": "\033[31m", "model": "\033[36m", "human": "\033[35m", "dim": "\033[2m", "end": "\033[0m"}


def paint(text: str, colour: str) -> str:
    import sys
    return f"{COLOURS[colour]}{text}{COLOURS['end']}" if sys.stdout.isatty() else text


def model_name(m) -> str:
    m = str(m or "")
    for key, label in (("super", "Nemotron-Super"), ("lightning", "Nemotron-Lightning"), ("nano", "Nemotron-Nano"),
                       ("ultra", "Nemotron-Ultra")):
        if key in m:
            return label + (" (replay)" if "replay" in m else "")
    return m or "rule-based"


def short(x) -> str:
    return str(x or "").split(":")[-1]


def describe_step(r: dict) -> str:
    d, ok, step = r.get("detail", {}), r.get("ok"), r.get("step") or r.get("agent")
    if step == "reflex":
        return f"fenced {d.get('fence_cells')} cells, stopped {len(d.get('stopped', []))} robot(s)"
    if step == "monitor":
        return f"{d.get('verdict')} (p={d.get('p_real')}): {d.get('summary', '')}"
    if step == "memory":
        return f"H-MEM lessons {d.get('lessons')}" if d.get("playbook") else "no past lessons"
    if step == "candidates":
        return f"people {[short(w) for w in d.get('workers', [])]}, robots {[short(a) for a in d.get('agvs', [])]}"
    if step == "planner":
        routes = ", ".join(f"{short(x['agv_id'])}->{x['to_station']} {x['speed_mps']}/{x.get('crossing_speed_mps')} m/s"
                           for x in d.get("agv_routes", []))
        return (f"{model_name(d.get('model'))} attempt {d.get('attempt')} -> send "
                f"{', '.join(short(a) for a in d.get('assignees', []))}{'; ' + routes if routes else ''}")
    if step == "critic" and "kept" in d:
        return f"kept {d.get('kept')} findings, dropped {len(d.get('dropped', []))}"
    if step == "critic":
        if ok:
            return "plan passes every safety rule"
        return "REJECTED: " + "; ".join(f"{v['rule_id']} {v['detail']} [{v['citations'][0]['ref'] if v.get('citations') else v['cfr_ref']}]"
                                         for v in d.get("violations", []))
    if step == "dryrun":
        return "60 s simulation ok" if ok else f"simulation problem: {d.get('reasons')}"
    if step == "restraint":
        return f"decision {d.get('decision')}: {(d.get('reasons') or [''])[-1]}"
    if step == "gate":
        return f"{d.get('gate')} ({d.get('autonomy')}): {'; '.join(d.get('reasons', []))}"
    if step == "executor":
        return f"dispatched, ETA {d.get('eta_s')}"
    if step == "coordinator":
        return f"{model_name(d.get('model'))} split the question into {[t['kind'] for t in d.get('tasks', [])]}"
    if step == "sql":
        return (f"{d.get('rows')} rows (attempt {d.get('attempt')}): {' '.join(str(d.get('sql', '')).split())[:110]}"
                if ok else f"failed: {str(d.get('error'))[:140]}")
    if step == "logs":
        s = d.get("summary", {})
        return f"{s.get('incidents_matched_in_traces')} traced incidents, decisions {s.get('decisions')}, {len(s.get('h_mem_lessons', []))} lessons"
    if step == "analyst":
        return f"{model_name(d.get('model'))}: {d.get('findings')} findings"
    if step == "verifier":
        return "world changed as predicted" if ok else f"MISMATCH {[(short(m['agent']), m['issue']) for m in d.get('mismatches', [])]}: re-planning"
    return json.dumps(d)[:160]


def follow(poll_s: float = 0.5) -> None:
    offsets: dict[Path, int] = {}
    start = time.time()
    print(f"Watching {LOGS}/ (Ctrl+C to stop). Start an incident in the UI or with run.py.\n", flush=True)
    while True:
        files = sorted(LOGS.glob("trace-*.jsonl")) + [LOGS / "llm.jsonl", LOGS / "approvals.jsonl"]
        for f in files:
            if not f.exists():
                continue
            if f not in offsets:  # only show what happens from now on
                offsets[f] = f.stat().st_size if f.stat().st_mtime < start else 0
            size = f.stat().st_size
            if size <= offsets[f]:
                continue
            with f.open() as fh:
                fh.seek(offsets[f])
                lines = fh.readlines()
                offsets[f] = fh.tell()
            for line in lines:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = time.strftime("%H:%M:%S")
                if f.name == "llm.jsonl":
                    t = r.get("tokens") or {}
                    print(paint(f"{stamp} NEMOTRON     {model_name(r.get('answering_model'))} {r.get('latency_s')}s "
                                f"({r.get('lane')} lane, {t.get('completion')} tokens)", "model"), flush=True)
                elif f.name == "approvals.jsonl":
                    print(paint(f"{stamp} HUMAN        {'approved' if r.get('approved') else 'rejected'}: {r.get('action')}",
                                "human"), flush=True)
                else:
                    step = r.get("step", "?")
                    secs = f"{r.get('ms', 0) / 1000:.1f}s" if (r.get("ms") or 0) >= 100 else f"{r.get('ms')}ms"
                    line = f"{stamp} {step.upper():<12} {secs:>7}  {describe_step(r)}"
                    print(paint(line, "ok" if r.get("ok") else "bad") + paint(f"  [{r.get('incident')}]", "dim"), flush=True)
        time.sleep(poll_s)


if __name__ == "__main__":
    try:
        follow()
    except KeyboardInterrupt:
        pass
