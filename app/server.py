"""Minimal web server, standard library only (http.server). No framework.

    python -m app.server [--port 8000] [--offline]

    GET  /             web/index.html
    GET  /api/state    grid, agents, geofence, last decision
    GET  /events       Server-Sent Events: trace steps, sim ticks, decisions, approvals
    GET  /health       health agent report (circuit breakers, floor vitals)
    GET  /api/knowledge what the agents know: World Model entities and checks, H-MEM lessons and ledger
    POST /shift/start  {"minutes", "seed", "rate", "workers"}: autonomous shift (Sentinel + concurrent incidents);
                       'shift', 'shift_event', 'shift_step', 'shift_done' events; GET /api/shift; GET /api/trace/<id>
    POST /shift/stop
    GET  /api/nemotron Nemotron scorecard: calls per role, latency, fallbacks, what the answers decided, receipts
    GET  /api/models   role -> model chain, breaker states and the last call per role (config/models.json)
    POST /ask          investigator team in the background -> 'ask_step' and 'ask_result' events; GET /api/ask_last
    GET  /api/asks     earlier questions, newest first; GET /api/ask/<id> one earlier answer (app/askhistory.py)
    POST /lab/run      Lab Level 2 experiment in the background -> 'lab' event; POST /lab/write saves it
    POST /lab/apply    puts the tested change on the floor; /lab/undo removes it; GET /api/lab_loop predicted vs
                       measured since (app/lab/loop.py); GET /api/lab_runs every experiment run, newest first
    POST /run          {"kind", "station", "agv", "severity", "note", "false_alarm", "stall_after", "seed"} -> runs the agent loop
    POST /approve      {"incident_id", "approve": true|false, "by"} -> a human judges the decision
    POST /autonomy     {"mode": "auto" | "supervised" | "advisory"} -> how much the agents may do on their own
    POST /api/run      JSON run contract (see app/api.py): runs one incident synchronously, returns JSON

Humans judge: an ACT plan is already executing, and rejecting it stops the agents involved. ESCALATE,
SAFE_HOLD and VERIFY wait for a person. Approving one executes its plan, and every judgement is logged
to logs/approvals.jsonl.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.agents.executor import execute
from app.engines.playbooks import PlaybookStore
from app.orchestrator import Orchestrator
from app.tracefmt import envelope, fill_targets
from app.sim.grid import Grid
from app.sim.sim import Sim

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LOGS = ROOT / "logs"
APPROVALS = LOGS / "approvals.jsonl"
TICK_INTERVAL_S = 0.5
class DuplicateIncident(RuntimeError):
    def __init__(self, existing: str, msg: str):
        super().__init__(msg)
        self.existing = existing


FINAL_GATES = ("approved", "rejected", "rolled_back", "confirmed")
DEDUP_WINDOW_S = 120  # a second report this soon after an incident was handled there is the same incident
FLOOR_WORKERS = 4  # incidents you run that work at once; more wait in the queue, people at risk jump it


class Bus:
    """Fan-out of events to every connected SSE client."""

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()

    def publish(self, kind: str, data: dict) -> None:
        msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        with self.lock:
            for q in list(self.clients):
                q.put(msg)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)



def _finished_after(meta: dict | None, since: datetime) -> bool:
    """An incident counts in a period when it finished in it (one without a finish time only counts under 'all')."""
    try:
        return datetime.fromisoformat((meta or {})["finished_at"]) >= since
    except (KeyError, TypeError, ValueError):
        return False


class App:
    def __init__(self, offline: bool = False, replay: bool = False):
        self.bus = Bus()
        self.replay = replay  # SAMPLE_MODE=true: every run starts from the recorded scenario state
        self.lock = threading.RLock()
        self.offline = offline
        self.degraded = None  # why the live server runs on the offline plant (Zetaris unreachable at start)
        if not offline:
            from app.engines.plant_world import load_plant_world
            from app.runtime import run_sql
            try:
                self.plant = load_plant_world(run_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")
                self.run_sql = run_sql
            except Exception as err:  # noqa: BLE001 - chaos test: Zetaris down at start used to crash the server
                self.degraded = f"Zetaris unreachable at start ({type(err).__name__}); running on the offline plant " \
                                "built from data/seed. Nightshift is still used. Restart once Zetaris is back."
                print(f"[WARN] {self.degraded}", flush=True)
                offline = self.offline = True
        if offline:
            from app.sim.offline import offline_plant
            self.plant, self.run_sql = offline_plant(), None
        self.grid = Grid.load(self.run_sql)
        self.floor_shift = "B"  # the crew placed on the simulated floor
        self.sim = Sim.from_plant(self.plant, self.grid, seed=42, shift=self.floor_shift)
        # every AGV dispatch also goes out as a VDA 5050 order (app/vda5050.py): the interface a real fleet speaks
        from app import vda5050
        self.vda = vda5050.Bridge(None if (replay or offline) else LOGS / "vda5050.jsonl")
        self._watch_invariants()
        # replay: an in-memory store per session, so a repeated incident can take the playbook route
        self.memory = PlaybookStore(path=None) if replay else PlaybookStore()
        self.mode = "dynamic"  # or "linear" (POST /run {"mode": "linear"}) for comparison
        self.traces: dict[str, object] = {}
        self.trace_meta: dict[str, dict] = {}  # incident id -> {source, finished_at}, for the Incidents table
        # handled incidents survive a restart (live only: replay must start from the recorded state)
        self.incidents_file = None if (replay or offline) else LOGS / "incidents.jsonl"
        self._persist_lock = threading.Lock()
        self._load_incidents()
        self.last_lab: dict | None = None
        self.lab_runs: list[dict] = []  # every experiment this server ran, newest last (so a repeat shows if it varies)
        from app.lab.loop import LabLoop
        self.lab_loop = LabLoop()  # Lab in the loop: the applied change and the incidents measured since
        self.lab_file = None if (replay or offline) else LOGS / "lab_state.json"  # survives a restart (live only)
        if self.lab_file is not None and self.lab_file.exists():
            try:
                st = json.loads(self.lab_file.read_text())
                self.lab_runs = st.get("runs", [])[-20:]
                self.lab_loop.applied, self.lab_loop.measured = st.get("applied"), st.get("measured", [])
            except (ValueError, OSError):
                pass
        from app.askhistory import AskHistory
        self.asks = AskHistory(LOGS / ("ask_history-replay.jsonl" if replay else "ask_history.jsonl"))
        self.last_ask: dict | None = self.asks.last()
        self.asking = False
        self.autonomy = "auto"
        self.busy = False  # only the linear comparison pipeline runs alone
        self.floor = None  # dispatcher for the incidents you run: several at once (_floor_dispatcher)
        from app.autonomy.leases import LeaseBook
        from app.sim.sim import TICK_S
        self.floor_leases = LeaseBook(ttl_s=300.0, clock=lambda: self.sim.tick * TICK_S)  # leases on floor time
        self.lease_of: dict[str, object] = {}  # incident -> the LeaseBook it took people and AGVs from
        self.floor_req: dict[str, dict] = {}  # incident -> its /run request (autonomy override)
        self.shift = None  # an autonomous shift in progress (app/autonomy/shift.py)
        self.always_on = None  # the last shift request while "keep watching" is on: the next shift starts by itself
        # durable, hash-chained journal (app/journal.py); approvals get their own chain. On start, recover what a
        # crash left open: interrupted incidents go to a person, their leases are void, fencing tokens move on.
        from app.journal import Journal
        # replay keeps its own files: a live and a replay server appending to one chain used to break it
        self.journal = Journal(LOGS / "journal-replay.jsonl" if replay else None, replicate=not replay and not offline)
        self.plant_stale = None  # set when a refresh of the plant snapshot failed: decisions use the last good one
        if not replay and not offline:  # live data changes (a certificate lapses, someone goes home): refresh it
            threading.Thread(target=self._refresh_plant_loop, daemon=True).start()
        if not replay and not offline:  # agent steps and model calls into Postgres, queried as sf_ops.* through Zetaris
            from app.logsync import sync_loop
            threading.Thread(target=sync_loop, daemon=True).start()
        if not replay:  # model self-check: who really answers each pinned model? (background, ~1 s per model)
            from app import models as _models
            threading.Thread(target=_models.self_check, daemon=True).start()
        self.audit = Journal(LOGS / "approvals-replay.jsonl" if replay else APPROVALS)
        rec = self.journal.recover()
        self.recovered = rec
        if rec["interrupted"] or rec["void_leases"] or rec["shift"]:
            self.journal.append("recovered", {"interrupted": [i["incident"] for i in rec["interrupted"]],
                                              "void_leases": [l["resource"] for l in rec["void_leases"]],
                                              "last_token": rec["last_token"],
                                              "note": "process restarted: these need a person"})
        self.shift_summary: dict | None = None
        threading.Thread(target=self._ticker, daemon=True).start()

    def _watch_invariants(self) -> None:
        """Independent runtime checks on the floor after every tick (app/sim/invariants.py)."""
        from app.sim.invariants import InvariantMonitor
        self.invariants = getattr(self, "invariants", None) or InvariantMonitor(
            self.plant, on_violation=lambda v: self.bus.publish("invariant", v))
        self.sim.monitor = self.invariants

    def llm(self):
        from app import runtime
        from app.fixtures import maybe_sample  # SAMPLE_MODE record/replay (no-op when it is off)
        return maybe_sample(runtime.chat_json, "json"), maybe_sample(runtime.chat, "text")

    def _ticker(self) -> None:
        while True:
            time.sleep(TICK_INTERVAL_S)
            with self.lock:
                if self.busy:
                    continue
                self.sim.step()
                self.bus.publish("tick", self.snapshot(full=False))
            self._approval_timeouts()
            if self.floor is not None and self.floor.queue:
                self.floor.step()  # a slot freed up: admit the next most urgent incident
            if self.shift is not None:
                self._shift_tick()

    def _refresh_plant_loop(self, every_s: float = 300.0) -> None:
        while True:
            time.sleep(every_s)
            self.refresh_plant()

    def refresh_plant(self) -> dict:
        """Reload the plant snapshot from Zetaris; on failure keep the last good one and say since when."""
        from app.engines.plant_world import load_plant_world
        try:
            fresh = load_plant_world(self.run_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")
        except Exception as err:  # noqa: BLE001 - stale but consistent beats a half-loaded world
            self.plant_stale = self.plant_stale or {"since": time.time(), "error": f"{type(err).__name__}: {str(err)[:120]}"}
            return {"ok": False, **self.plant_stale}
        changed = getattr(fresh, "version", None) != getattr(self.plant, "version", None)
        with self.lock:  # incidents already running keep the snapshot they started with
            self.plant, self.plant_stale = fresh, None
        if changed:
            self.journal.append("plant_refreshed", {"version": fresh.version})
        return {"ok": True, "version": fresh.version, "changed": changed}

    def _approval_timeouts(self) -> None:
        """A person who does not answer: remind, escalate, then decide safely (app/autonomy/approval.py)."""
        from app.autonomy.approval import next_action
        from app.autonomy.sentinel import TICKS_PER_MIN
        for trace in list(self.traces.values()):
            if trace.gate not in ("awaiting_approval", "advisory"):
                continue
            if trace.waiting_since_tick is None:
                trace.waiting_since_tick = self.sim.tick
                continue
            waited = (self.sim.tick - trace.waiting_since_tick) / TICKS_PER_MIN
            action = next_action(trace, waited, trace.autonomy)
            if action:
                self.timeout_action(trace, action, waited)

    def timeout_action(self, trace, action: str, waited: float) -> dict:
        from app.orchestrator import executable_plan
        trace.escalations.append(action)
        note = {"remind": "reminder sent to the approver",
                "escalate": "escalated to the backup approver (shift manager)",
                "auto_execute": "no answer: plan dispatched automatically (only the Supervised setting held it; "
                                "critic and dry run passed, acting is cheaper than waiting)",
                "hold": "no answer: safe hold kept (fence and stopped AGVs stay), reserved people and robots "
                        "released, still escalated; nothing that needs a person's judgement was dispatched"}[action]
        with self.lock:
            if action == "auto_execute":
                plan = executable_plan(trace)
                if plan is not None:
                    for aid in list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]:
                        if aid in self.sim.agents:
                            trace.dispatch.setdefault(aid, list(self.sim.agents[aid].pos))
                    execute(plan, trace.incident, self.plant, self.sim, fleet=self._fleet())
                    trace.gate = "auto_after_timeout"
            elif action == "hold":
                trace.gate = "timeout_hold"
                self._release(trace.incident.id)
        if action in ("auto_execute", "hold"):
            self._persist(trace.incident.id)
            if trace.gate == "auto_after_timeout":
                self._measure(trace)  # dispatched now: the Lab's applied change applies to it
        record = {"ts": datetime.now().astimezone().isoformat(), "incident": trace.incident.id, "approved": None,
                  "by": "approval-timeout policy", "decision": trace.decision.decision if trace.decision else None,
                  "gate": trace.gate, "action": note, "waited_min": round(waited, 1)}
        if action in ("auto_execute", "hold"):
            ev = self.audit.append("approval", record)
            record["audit_hash"] = ev["hash"][:16]
        self.journal.append("approval_timeout", {**record, "step": action})
        self.bus.publish("approval_timeout", record)
        self.bus.publish("gate", {"incident": trace.incident.id, "gate": trace.gate})
        return record

    def snapshot(self, full: bool = True) -> dict:
        agents = [{"id": a.id, "kind": a.kind, "x": a.pos[0], "y": a.pos[1], "status": a.status}
                  for a in self.sim.agents.values()]
        last = list(self.traces.values())[-1] if self.traces else None
        involved = sorted(last.dispatch) if last else []
        held = {l["resource"] for l in self.floor_leases.snapshot()}  # every resource an open incident holds
        if self.shift is not None:
            held |= {l["resource"] for l in self.shift.leases.snapshot()}
        if held or self.shift is not None:
            involved = sorted(held)
        out = {"tick": self.sim.tick, "agents": agents, "geofence": sorted(self.sim.geofence),
               "collisions": len(self.sim.collisions), "incident_agents": involved, "autonomy": self.autonomy}
        if full:
            out["grid"] = {"cols": self.grid.cols, "rows": self.grid.rows,
                           "cells": [[x, y, label] for (x, y), label in sorted(self.grid.cells.items())]}
            out["stations"] = [{"id": e["id"].split(":")[-1], "name": e["name"], "x": e["attrs"]["x"], "y": e["attrs"]["y"]}
                               for e in self.plant.entities("station")]
            out["offline"] = self.offline
        return out

    def run(self, req: dict) -> str:
        """Start an incident you reported. Several run at once: each goes to the floor dispatcher (most urgent first,
        each on its own dynamic route) and leases the people and AGVs it sends, so two incidents never claim the same
        one. During an autopilot shift they share the shift's leases. Only the linear comparison pipeline runs alone."""
        linear = (req.get("mode") or self.mode) == "linear"
        with self.lock:
            if self.busy:
                raise RuntimeError("a linear-pipeline comparison run is in progress: it runs alone, wait for it")
            from app.api import KINDS, SEVERITIES
            if req.get("kind") not in KINDS:
                raise ValueError(f"kind must be one of {list(KINDS)}")
            if req.get("severity") not in (None, "", *SEVERITIES):
                raise ValueError(f"severity must be one of {list(SEVERITIES)}")
            agv = f"agv:plant:{req['agv']}" if req.get("agv") else None
            if req["kind"] == "agv_fault" and agv not in self.sim.agents:
                raise ValueError("an AGV fault needs a known AGV, e.g. AGV-3")
            if req["kind"] != "agv_fault" and not any(e["id"].split(":")[-1] == req.get("station")
                                                      for e in self.plant.entities("station")):
                raise ValueError(f"unknown station {req.get('station')!r}")
            open_ids = self._open_ids()
            dup = self._duplicate_of(req["kind"], req.get("station"), agv, open_ids)
            if dup:  # one incident per kind and place: a second report of it is the same incident
                raise DuplicateIncident(dup, f"{dup} is already handling this {req['kind'].replace('_', ' ')} at "
                                             f"{req.get('agv') or req.get('station')}: opened it instead of a duplicate")
            if linear and open_ids:
                raise RuntimeError("the linear pipeline is a comparison that runs alone: wait for the open incidents")
            if (req.get("seed") is not None or self.replay) and not open_ids:  # a fresh, seeded floor when idle
                seq = self.sim._incident_seq
                self.sim = Sim.from_plant(self.plant, self.grid, seed=int(req.get("seed") or 42), shift="B")
                self.sim._incident_seq = seq  # ids keep counting, so every incident has its own (replay: see fixtures)
                self._watch_invariants()
            for key in list(self.sim.fences):  # fences of incidents that are over go; open incidents keep theirs
                if key not in open_ids:
                    self.sim.clear_geofence(key)
            self.busy = linear
            try:
                inc = self.sim.inject(req["kind"], self.plant, station_id=req.get("station"), agv_id=agv,
                                      false_alarm=bool(req.get("false_alarm")), severity=req.get("severity") or None,
                                      note=req.get("note"))
            except Exception:
                self.busy = False
                raise
        from app.agents.textguard import screen
        flagged = screen(req.get("note"))[1]
        if flagged:  # the agents see a placeholder; the person reading the incident sees the original and why
            self.bus.publish("note_flagged", {"incident": inc.id, "original": str(req.get("note"))[:300], "matched": flagged})
            self.journal.append("note_flagged", {"incident": inc.id, "matched": flagged})
        self.bus.publish("incident", inc.model_dump(exclude={"truth_false_alarm"}))
        if linear:
            threading.Thread(target=self._run, args=(inc, req), daemon=True).start()
            return inc.id
        stall = (0, int(req["stall_after"])) if req.get("stall_after") not in (None, "") else None
        self.floor_req[inc.id] = req
        floor = self._floor_dispatcher()
        floor.sim = self.sim
        floor.submit(inc, detected_tick=self.sim.tick, stall=stall)
        floor.step()  # threaded: admits it now (or queues it behind more urgent ones)
        return inc.id

    def _duplicate_of(self, kind: str, station: str | None, agv: str | None, open_ids: set[str]) -> str | None:
        """An incident of the same kind at the same place (or on the same AGV) that is still open: running, queued,
        waiting for a person, or dispatched with its responders still on the way (or finished moments ago)."""
        cands = []
        for d in (self.floor, self.shift.dispatcher if self._shift_running() else None):
            if d is not None:
                with d.lock:
                    cands += [e["incident"] for e in d.active.values()] + [q[2] for q in d.queue]
        cands += [t.incident for t in self.traces.values() if t.gate in ("awaiting_approval", "advisory", "timeout_hold")]
        now = datetime.now().astimezone()
        for iid, t in list(self.traces.items()):  # dispatched: the response is still under way on the floor
            if t.gate not in ("auto_executed", "approved", "confirmed", "auto_after_timeout"):
                continue
            on_way = any(a in self.sim.agents and self.sim.agents[a].status in ("moving", "stalled") for a in t.dispatch)
            done = (self.trace_meta.get(iid) or {}).get("finished_at")
            recent = done and (now - datetime.fromisoformat(done)).total_seconds() < DEDUP_WINDOW_S
            if on_way or recent:
                cands.append(t.incident)
        for inc in cands:
            if inc.kind == kind and ((agv and getattr(inc, "agv_id", None) == agv) or (not agv and inc.station_id == station)):
                return inc.id
        return None

    def _shift_running(self) -> bool:
        return self.shift is not None and self.shift_summary is None

    def _open_ids(self) -> set[str]:
        """Incidents still being worked on, here or in the shift."""
        ids = set()
        for d in (self.floor, self.shift.dispatcher if self._shift_running() else None):
            if d is not None:
                with d.lock:
                    ids |= set(d.active) | {q[2].id for q in d.queue}
        return ids

    def _floor_dispatcher(self):
        if self.floor is None:
            from app.autonomy.dispatcher import Dispatcher

            def event(kind, data):
                if kind == "error":
                    self.bus.publish("error", data)
                self.bus.publish("floor", {"kind": kind, **{k: v for k, v in data.items() if k != "route"}})
            self.floor = Dispatcher(self._make_floor_orch, self.plant.stats, workers=FLOOR_WORKERS, threads=True,
                                    on_event=event, on_done=self._floor_done, sim=self.sim)
        return self.floor

    def _fleet(self):
        """The fleet connector on the current sim (replay rebuilds the sim per run): AGV moves go out as VDA 5050."""
        from app import vda5050
        from app.connectors import SimFleet
        return vda5050.Fleet(SimFleet(self.sim), self.vda, self.sim)

    def _make_floor_orch(self, inc, risk):
        from app.dynamic import DynamicOrchestrator
        from app.investigate.sources import sql_runner
        req = self.floor_req.get(inc.id, {})
        leases = self.shift.leases if self._shift_running() else self.floor_leases
        self.lease_of[inc.id] = leases
        llm_json, llm_text = self.llm()
        orch = DynamicOrchestrator(self.plant, self.sim, llm_json, llm_text, memory=self.memory, run_id=inc.id,
                                   autonomy=req.get("autonomy") or self.autonomy, echo=True,
                                   on_step=lambda iid, st: self.bus.publish("step", {"incident": iid, **st.model_dump()}),
                                   router_llm=None if self.replay else llm_json,
                                   run_sql=sql_runner("true") if self.offline else self.run_sql,
                                   leases=leases, risk=risk, sim_lock=self.lock, fleet=self._fleet())
        if self._shift_running():
            orch.fleet, orch.mes = self.shift.fleet, self.shift.mes
        return orch

    def _floor_done(self, trace, result) -> None:
        iid = result["incident"]
        self.floor_req.pop(iid, None)
        if trace is None or trace.gate != "awaiting_approval":  # a plan waiting for a person keeps its people and AGVs
            leases = self.lease_of.pop(iid, None)
            if leases is not None:
                leases.release_all(iid)
        if trace is None:
            return
        if self.memory is not None:
            self.memory.record(trace)
        self._store(trace, "you ran it", result.get("seconds"))

    def _load_incidents(self, keep: int = 2000) -> None:
        from app.contracts import Trace
        path = self.incidents_file
        if path is None or not path.exists():
            return
        rows: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                rows.pop(r["id"], None)
                rows[r["id"]] = r  # the last line for an id wins (a later approval rewrites it)
            except (ValueError, KeyError):
                continue
        seq = self.sim._incident_seq
        for iid, r in list(rows.items())[-keep:]:
            try:
                self.traces[iid] = Trace.model_validate(r["trace"])
            except Exception:  # noqa: BLE001 - an old line from before a schema change: skip it
                continue
            self.trace_meta[iid] = r.get("meta") or {}
            m = re.fullmatch(rf"SIM-{self.sim.seed}-(\d+)", iid)
            seq = max(seq, int(m.group(1))) if m else seq
        self.sim._incident_seq = seq  # new incidents keep counting after the loaded ones

    def _persist(self, iid: str) -> None:
        t = self.traces.get(iid)
        if self.incidents_file is None or t is None:
            return
        try:
            with self._persist_lock, self.incidents_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": iid, "meta": self.trace_meta.get(iid, {}), "trace": t.model_dump(mode="json")},
                                    default=str) + "\n")
        except OSError as err:
            print(f"[WARN] could not save incident {iid}: {err}", flush=True)

    def _store(self, trace, source: str, seconds: float | None = None) -> None:
        inc = trace.incident
        if inc.id in self.traces:  # replay rebuilds the sim, so ids repeat: keep the earlier run under its own key
            n = 1 + sum(k.startswith(inc.id + " (run ") for k in self.traces)
            self.traces[f"{inc.id} (run {n})"] = self.traces.pop(inc.id)
            self.trace_meta[f"{inc.id} (run {n})"] = self.trace_meta.pop(inc.id, {})
        self.traces[inc.id] = trace
        self._measure(trace)
        now = datetime.now().astimezone()
        self.trace_meta[inc.id] = {"source": source, "finished_at": now.isoformat(), "seconds": seconds,
                                   "started_at": (now - timedelta(seconds=seconds)).isoformat() if seconds else None}
        self._persist(inc.id)
        self.bus.publish("decision", {"incident": inc.id, "decision": trace.decision.model_dump() if trace.decision else None,
                                      "plan": trace.plan.model_dump() if trace.plan else None,
                                      "critic_rejections": trace.critic_rejections, "replans": trace.replans,
                                      "safe_mode": trace.safe_mode, "gate": trace.gate,
                                      "gate_reasons": trace.gate_reasons, "autonomy": trace.autonomy,
                                      "mode": trace.mode, "path": trace.path, "kind": inc.kind,
                                      "where": inc.station_id or inc.zone_id})

    def floor_state(self) -> dict:
        """The incidents you ran that are still open (running or queued), for the Live floor list."""
        d = self.floor
        if d is None:
            return {"active": [], "queue": [], "leases": []}
        active = []
        for e in list(d.active.values()):
            t = getattr(e["orch"], "trace", None)
            active.append({"incident": e["incident"].id, "kind": e["incident"].kind,
                           "station": e["incident"].station_id or e["incident"].zone_id, "risk": e["risk"],
                           "hops": e["hops"], "at": t.steps[-1].step if t and t.steps else "starting"})
        return {"active": active, "queue": d.queued(), "leases": self.floor_leases.snapshot(), "workers": d.workers}

    def live_snapshot(self) -> dict:
        """The orchestrator's view for the Ask coordinator: what is open, what waits for a person, and the fleet."""
        opened, seen = [], set()
        for a in self.floor_state()["active"]:
            opened.append({"incident": a["incident"], "kind": a["kind"], "station": a["station"], "state": "agents working"})
        for q in self.floor_state()["queue"]:
            opened.append({"incident": q["incident"], "kind": q["kind"], "station": q["station"], "state": "queued"})
        waiting = {"awaiting_approval": "waiting for your approval", "advisory": "waiting: advisory, you dispatch",
                   "timeout_hold": "waiting: held after a timeout"}
        for iid, t in list(self.traces.items()):
            if t.gate in waiting and " (run " not in iid:
                opened.append({"incident": iid, "kind": t.incident.kind,
                               "station": t.incident.station_id or t.incident.zone_id, "state": waiting[t.gate]})
        opened = [o for o in opened if not (o["incident"] in seen or seen.add(o["incident"]))]
        agvs = [a for a in self.sim.agents.values() if a.kind == "agv"]
        bad = [{"agv": a.id.split(":")[-1], "status": a.status} for a in agvs if a.status in ("fault", "stalled", "held")]
        from app.autonomy.detectors import shift_of_hour
        now = datetime.now().astimezone()
        return {"time": now.strftime("%H:%M"), "hour": now.hour, "shift": shift_of_hour(now.hour),
                "floor_shift": getattr(self, "floor_shift", "B"),  # the crew the floor sim was built with
                "open": opened, "agvs": {"total": len(agvs), "available": len(agvs) - len(bad), "problems": bad},
                "autopilot": self.shift is not None and self.shift_summary is None}

    def recent_asks(self, minutes: int = 30) -> list[dict]:
        """This conversation: the last few questions asked within the last half hour, oldest first."""
        cutoff = datetime.now().astimezone() - timedelta(minutes=minutes)
        out = []
        for r in self.asks.listing()[:3]:
            try:
                if datetime.fromisoformat(r["asked_at"]) >= cutoff:
                    out.append({"question": r["question"], "summary": r["summary"]})
            except (TypeError, ValueError):
                continue
        return out[::-1]

    def _release(self, iid: str) -> None:
        leases = self.lease_of.pop(iid, None) or (self.shift.leases if self.shift is not None else None)
        if leases is not None:
            leases.release_all(iid)

    def _run(self, inc, req) -> None:
        """The linear comparison pipeline (--linear): one incident, the whole floor locked."""
        try:
            llm_json, llm_text = self.llm()
            orch = Orchestrator(self.plant, self.sim, llm_json, llm_text, memory=self.memory, run_id=inc.id,
                                autonomy=req.get("autonomy") or self.autonomy, echo=True,
                                on_step=lambda iid, st: self.bus.publish("step", {"incident": iid, **st.model_dump()}))
            stall = (0, int(req["stall_after"])) if req.get("stall_after") not in (None, "") else None
            started = time.perf_counter()
            with self.lock:
                trace = orch.handle(inc, stall=stall)
            self._store(trace, "you ran it (linear)", round(time.perf_counter() - started, 2))
        except Exception as err:  # noqa: BLE001 - report to the UI and keep serving
            self.bus.publish("error", {"incident": inc.id, "error": f"{type(err).__name__}: {err}"[:300]})
        finally:
            self.busy = False

    # ---------------------------------------------------------------- autonomous shift

    def start_shift(self, req: dict) -> dict:
        from app.autonomy.shift import Shift
        from app.dynamic import DynamicOrchestrator
        from app.investigate.sources import sql_runner
        if self.busy or (self.shift is not None and not self.shift.finished()):
            raise RuntimeError("an incident or a shift is already running")
        if self.floor is not None and not self.floor.idle():  # they lease from another book than the shift's
            raise RuntimeError("incidents you ran are still open: wait for them (or judge them), then start the shift")
        minutes = min(max(float(req.get("minutes") or 8), 1), 60)
        seed, rate = int(req.get("seed") or 7), min(max(float(req.get("rate") or 2.5), 0.2), 8)
        workers = min(max(int(req.get("workers") or 2), 1), 6)
        self.always_on = {"minutes": minutes, "seed": seed, "rate": rate, "workers": workers} \
            if req.get("continuous") else None
        llm_json, llm_text = self.llm()
        run_sql = sql_runner("true") if self.offline else self.run_sql
        stamp = int(time.time())

        def make(inc, risk, leases, lock):
            return DynamicOrchestrator(self.plant, self.sim, llm_json, llm_text, memory=self.memory, run_id=inc.id,
                                       autonomy=self.autonomy, router_llm=None if self.replay else llm_json,
                                       run_sql=run_sql, leases=leases, risk=risk, sim_lock=lock,
                                       trace_path=LOGS / f"trace-shift-{seed}-{stamp}-{inc.id}.jsonl",
                                       on_step=lambda iid, st: self.bus.publish("shift_step", {"incident": iid, **st.model_dump()}))

        def event(kind, data):
            self.bus.publish("shift_event", {"kind": kind, **data})

        with self.lock:
            self.sim.fences.clear()
            self.shift = Shift(self.plant, self.sim, make, minutes=minutes, seed=seed, workers=workers,
                               threads=not self.replay, sentinel_llm=None if self.replay else llm_json,
                               on_event=event, sim_lock=self.lock, memory=self.memory, rate_per_min=rate,
                               journal=self.journal)
            from app import vda5050
            self.shift.fleet = vda5050.Fleet(self.shift.fleet, self.vda, self.sim)  # shift AGV orders as VDA 5050
            self.shift.begin()
        self.shift_summary = None
        original = self.shift._done

        def done(trace, result):
            original(trace, result)
            if trace is not None:
                self.traces[trace.incident.id] = trace
                now = datetime.now().astimezone()
                self.trace_meta[trace.incident.id] = {"source": "autopilot shift", "finished_at": now.isoformat(),
                                                      "seconds": result.get("seconds"),
                                                      "started_at": (now - timedelta(seconds=result.get("seconds") or 0)).isoformat()}
                self._measure(trace)
                self._persist(trace.incident.id)

        self.shift.dispatcher.on_done = done
        self.bus.publish("shift_event", {"kind": "shift_started", "minutes": minutes, "seed": seed,
                                         "signals": len(self.shift.signals)})
        return {"status": "running", "minutes": minutes, "seed": seed, "signals": len(self.shift.signals)}

    def _shift_tick(self) -> None:
        sh = self.shift
        if sh is None or self.shift_summary is not None:
            return
        try:
            sh.poll()
        except Exception as err:  # noqa: BLE001 - keep the floor running
            self.bus.publish("error", {"incident": "shift", "error": f"{type(err).__name__}: {err}"[:300]})
        if sh.finished():
            self.shift_summary = sh.end()
            self.bus.publish("shift_done", self.shift_summary)
            if self.always_on and not sh.stopped:  # keep watching: the next shift (next seed) starts at once
                nxt = {**self.always_on, "seed": self.always_on["seed"] + 1, "continuous": True}
                threading.Thread(target=self._restart_shift, args=(nxt,), daemon=True).start()
        self.bus.publish("shift", self.shift_state())

    def _restart_shift(self, req: dict) -> None:
        try:
            self.start_shift(req)
        except Exception as err:  # noqa: BLE001 - shown to the user; watching stops until they start it again
            self.always_on = None
            self.bus.publish("error", {"incident": "shift", "error": f"always-on restart: {err}"[:300]})

    def stop_shift(self) -> dict:
        if self.shift is None:
            raise RuntimeError("no shift is running")
        self.always_on = None  # Stop also ends "keep watching"
        self.shift.stopped = True
        return {"status": "stopping"}

    def shift_state(self) -> dict:
        sh = self.shift
        if sh is None:
            return {"running": False}
        from app.autonomy.sentinel import TICKS_PER_MIN
        active = []
        for e in list(sh.dispatcher.active.values()):
            t = getattr(e["orch"], "trace", None)
            last = t.steps[-1].step if t and t.steps else "queued"
            active.append({"incident": e["incident"].id, "kind": e["incident"].kind, "station": e["incident"].station_id,
                           "risk": e["risk"], "hops": e["hops"], "at": last})
        done = sh.dispatcher.done[-40:]
        return {"running": self.shift_summary is None, "minutes": sh.minutes, "seed": sh.seed,
                "elapsed_min": round((self.sim.tick - sh.start_tick) / TICKS_PER_MIN, 1),
                "signals": len(sh.signals), "signals_seen": sh.next_signal, "sentinel": dict(sh.sentinel.stats),
                "queue": sh.dispatcher.queued(), "active": active, "leases": sh.leases.snapshot(),
                "done": [{k: r[k] for k in ("incident", "kind", "station", "risk", "decision", "gate", "route")}
                         | {"gate": self.traces[r["incident"]].gate if r["incident"] in self.traces else r["gate"]}
                         for r in done],
                "summary": {k: v for k, v in (self.shift_summary or {}).items() if k != "results"} or None}

    def _running_trace(self, incident_id: str):
        for d in (self.floor, self.shift.dispatcher if self.shift is not None else None):
            e = d.active.get(incident_id) if d is not None else None
            if e is not None and getattr(e["orch"], "trace", None) is not None:
                return e["orch"].trace
        return None

    def trace_of(self, incident_id: str) -> dict:
        trace = self.traces.get(incident_id)
        running = trace is None and (trace := self._running_trace(incident_id)) is not None
        if trace is None:
            raise KeyError("unknown incident")
        return {"incident": trace.incident.model_dump(exclude={"truth_false_alarm"}),
                "steps": [st.model_dump() for st in trace.steps], "path": trace.path, "gate": trace.gate,
                "decision": trace.decision.model_dump() if trace.decision else None,
                "plan": trace.plan.model_dump() if trace.plan else None, "gate_reasons": trace.gate_reasons,
                "safe_mode": trace.safe_mode, "critic_rejections": trace.critic_rejections, "replans": trace.replans,
                "mode": trace.mode, "running": running, "from_shift": self.shift is not None and any(
                    r["incident"] == incident_id for r in self.shift.dispatcher.done)}

    def incidents(self) -> list[dict]:
        """Every incident this server has handled, newest first: one row each for the Incidents table."""
        out = []
        for iid, t in self.traces.items():
            calls = [st.detail.get("model_call") for st in t.steps if isinstance(st.detail.get("model_call"), dict)]
            calls += [st.detail.get("route", {}).get("model_call") for st in t.steps
                      if isinstance(st.detail.get("route", {}).get("model_call"), dict)]
            meta = self.trace_meta.get(iid, {})
            out.append({"incident": iid, "kind": t.incident.kind, "station": t.incident.station_id or t.incident.zone_id,
                        "severity": t.incident.severity, "source": meta.get("source", "-"),
                        "finished_at": meta.get("finished_at"), "started_at": meta.get("started_at"),
                        "wall_s": meta.get("seconds"), "decision": t.decision.decision if t.decision else None,
                        "gate": t.gate, "steps": len(t.steps), "hops": len(t.path), "model_calls": len(calls),
                        "ms": round(sum(st.ms for st in t.steps)), "safe_mode": t.safe_mode,
                        "critic_rejections": t.critic_rejections})
        done = sorted(out, key=lambda r: r["finished_at"] or "", reverse=True)
        return self._open_rows({r["incident"] for r in done}) + done

    def _open_rows(self, listed: set[str]) -> list[dict]:
        """Incidents still being worked on or waiting in a queue: listed at the top as soon as they are reported."""
        rows = []
        for d, source in ((self.floor, "you ran it"), (self.shift.dispatcher if self.shift is not None else None,
                                                       "autopilot shift")):
            if d is None:
                continue
            for e in list(d.active.values()):
                inc, t = e["incident"], getattr(e["orch"], "trace", None)
                if inc.id in listed:
                    continue
                steps = t.steps if t is not None else []
                rows.append({"incident": inc.id, "kind": inc.kind, "station": inc.station_id or inc.zone_id,
                             "severity": inc.severity, "source": source, "started_at": None, "finished_at": None,
                             "wall_s": None, "decision": None, "gate": "running", "steps": len(steps),
                             "hops": len(t.path) if t is not None else 0, "model_calls": 0,
                             "ms": round(sum(st.ms for st in steps)), "safe_mode": False, "critic_rejections": 0,
                             "at": steps[-1].step if steps else "starting"})
            for q in d.queued():
                if q["incident"] not in listed:
                    rows.append({"incident": q["incident"], "kind": q["kind"], "station": q["station"], "severity": "-",
                                 "source": source, "started_at": None, "finished_at": None, "wall_s": None,
                                 "decision": None, "gate": "queued", "steps": 0, "hops": 0, "model_calls": 0, "ms": 0,
                                 "safe_mode": False, "critic_rejections": 0})
        return rows

    def incident_history(self, incident_id: str) -> dict:
        """Everything that happened in one incident: the trace (every agent step with who chose it, its model call and
        output), the decision and gate, and every human or timeout action on it from the audit trail."""
        out = self.trace_of(incident_id)
        since = self.trace_meta.get(incident_id, {}).get("finished_at", "")
        since = since[:19] if since else ""
        actions = [ev["data"] for ev in self.audit.events() if ev.get("kind") == "approval"
                   and (ev.get("data") or {}).get("incident") == incident_id and ev.get("ts", "")[:19] >= since[:17]]
        from app.stepio import step_inputs
        trace = self.traces.get(incident_id) or self._running_trace(incident_id)
        return {**out, "meta": self.trace_meta.get(incident_id, {}), "human_actions": actions,
                "inputs": step_inputs(trace)}

    def approve(self, req: dict) -> dict:
        """The human gate. Waiting/advisory plans run only on Approve; an auto-executed plan can be rejected,
        which stops its agents and sends them back where they started (roll-back)."""
        from app.orchestrator import executable_plan
        trace = self.traces.get(req.get("incident_id"))
        if trace is None:
            raise KeyError("unknown incident")
        approved = bool(req.get("approve"))
        action = "none"
        if trace.gate in FINAL_GATES:  # judged once: a second click must not record (or dispatch) anything again
            raise ValueError(f"already decided: {trace.gate.replace('_', ' ')}")
        with self.lock:
            plan = executable_plan(trace)
            stale = self._stale_reason(trace, plan) if trace.gate == "timeout_hold" and approved and plan else None
            if stale:  # a late answer after a timed-out hold: the floor may have moved on
                action = f"late approval refused: {stale}; re-run the incident for a fresh plan"
            elif trace.gate in ("awaiting_approval", "advisory", "timeout_hold"):  # a late answer still counts
                if approved and plan is not None:
                    for aid in list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]:
                        if aid in self.sim.agents:
                            trace.dispatch.setdefault(aid, list(self.sim.agents[aid].pos))
                    execute(plan, trace.incident, self.plant, self.sim, fleet=self._fleet())
                    trace.gate, action = "approved", "plan dispatched after human approval"
                    self._measure(trace)
                else:
                    trace.gate, action = "rejected", "plan dropped by the human; nothing was dispatched"
            elif trace.gate in ("auto_executed", "auto_after_timeout") and not approved:
                for aid, start in trace.dispatch.items():
                    if aid in self.sim.agents:
                        self.sim.stop(aid)
                        if tuple(start) != self.sim.agents[aid].pos:
                            self.sim.assign(aid, tuple(start), 1.0, 0.5 if self.sim.agents[aid].kind == "agv" else None)  # roll back
                trace.gate, action = "rolled_back", "auto-executed plan rolled back: agents stopped and sent back"
            elif approved and trace.gate in ("auto_executed", "auto_after_timeout"):
                trace.gate, action = "confirmed", "auto-executed plan confirmed by the human"
            self._persist(req.get("incident_id"))  # the judged state survives a restart too
            if trace.gate in ("rejected", "rolled_back"):
                self._release(trace.incident.id)  # the people and robots are free again
        self.bus.publish("gate", {"incident": trace.incident.id, "gate": trace.gate})
        record = {"ts": datetime.now().astimezone().isoformat(), "incident": trace.incident.id, "approved": approved,
                  "by": req.get("by", "supervisor"), "decision": trace.decision.decision if trace.decision else None,
                  "gate": trace.gate, "action": action}
        ev = self.audit.append("approval", record)  # hash-chained (HMAC with JOURNAL_HMAC_KEY): tamper-evident
        record["audit_hash"] = ev["hash"][:16]
        self.journal.append("approval", {**record})
        self.bus.publish("approval", record)
        return record

    def _stale_reason(self, trace, plan) -> str | None:
        """Before a late approval runs: the hold released this incident's people and robots, so check again."""
        from app.agents.safety_critic import review
        rules = [v.rule_id for v in review(plan, trace.incident, self.plant, self.sim).violations
                 if v.rule_id != "R-UNVERIFIED"]
        if rules:
            return f"the plan now breaks {', '.join(sorted(set(rules)))}"
        if self.shift is not None:
            ids = list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]
            taken = [l.resource.split(":")[-1] for l in self.shift.leases.conflicts(trace.incident.id, 0.0, ids)]
            if taken:
                return f"{', '.join(taken)} now work on another incident"
            self.shift.leases.acquire(trace.incident.id, 0.0, ids)
        return None

    def last(self) -> dict:
        """The most recent incident's trace, so a freshly opened page shows it too."""
        if not self.traces:
            return {}
        trace = list(self.traces.values())[-1]
        return {"incident": trace.incident.model_dump(exclude={"truth_false_alarm"}),
                "steps": [st.model_dump() for st in trace.steps],
                "decision": {"incident": trace.incident.id, "decision": trace.decision.model_dump() if trace.decision else None,
                             "plan": trace.plan.model_dump() if trace.plan else None,
                             "critic_rejections": trace.critic_rejections, "replans": trace.replans,
                             "safe_mode": trace.safe_mode, "gate": trace.gate,
                             "gate_reasons": trace.gate_reasons, "autonomy": trace.autonomy,
                             "mode": trace.mode, "path": trace.path}}

    def knowledge(self) -> dict:
        """What the agents know: the plant World Model (with live sim state) and the H-MEM lessons."""
        facts = lambda eid: {f["predicate"]: f["value"] for f in self.plant.world.view("facts").about(eid)  # noqa: E731
                             if not f.get("supersededBy")}
        agvs = []
        for e in self.plant.entities("agv"):
            agent = self.sim.agents.get(e["id"])
            agvs.append({"id": e["id"].split(":")[-1], "model": e["attrs"].get("model"), "status": agent.status if agent else "-",
                         "battery_health_pct": e["attrs"].get("battery_health_pct"), "faults_90d": e["attrs"].get("faults_90d"),
                         "service_overdue": facts(e["id"]).get("service_overdue", False)})
        workers = []
        for e in self.plant.entities("worker"):
            if e["id"] not in self.sim.agents:
                continue
            certs = {c: self.plant.cert_valid(e, c) for c in sorted(e["attrs"].get("cert_expiry", {}))}
            workers.append({"id": e["id"].split(":")[-1], "name": e["name"], "role": e["attrs"]["role"],
                            "skills": e["attrs"]["skills"], "certs": certs, "status": self.sim.agents[e["id"]].status})
        world = self.plant.world
        out = {"engine": "Meterless World Model", "events": len(world.events()), "audit": len(world.audit()),
               "loaded_at": getattr(self.plant, "loaded_at", None),
               "counts": {t: len(self.plant.entities(t)) for t in ("station", "machine", "worker", "agv")},
               "agvs": agvs, "workers_on_shift": workers,
               "rules": [{"id": k, "cfr_ref": v["cfr_ref"], "summary": v["summary"]} for k, v in sorted(self.plant.rules.items())],
               "memory": None}
        if self.memory is not None:
            recs = sorted(self.memory.hmem.store.all(), key=lambda m: (-m["confidence"], m["id"]))
            out["memory"] = {
                "engine": "Meterless H-MEM",
                "lessons": [{"id": m["id"], "content": m["content"], "confidence": round(m["confidence"], 3),
                             "access": m["accessCount"], "layer": m["layer"], "source": m["source"],
                             "tags": m["tags"]} for m in recs[:20]],
                "playbooks": [{"key": b["key"], "outcomes": b["outcomes"], "success_rate": b["success_rate"],
                               "lessons": [l["rule_id"] for l in b["lessons"]]} for b in self.memory.playbooks.values()],
                "ledger": self.memory.hmem.ledger.all()[-8:]}
        return out

    def lab(self) -> None:
        """Run Lab Level 2 in the background: each stage as a 'lab_step' event (and a JSONL trace line), the result as
        a 'lab' event. One experiment at a time."""
        if getattr(self, "lab_running", False):
            raise RuntimeError("an experiment is already running")
        self.lab_running = True
        lab_id = f"lab-{int(time.time())}-{os.urandom(2).hex()}"
        trace = Path(__file__).resolve().parents[1] / "logs" / f"trace-{lab_id}.jsonl"

        prev = [None]

        def on_step(name, started, detail):
            rec = {"lab_id": lab_id, "step": name, "ms": round((time.perf_counter() - started) * 1000, 1), "detail": detail}
            with trace.open("a", encoding="utf-8") as fh:
                env = envelope(lab_id, "lab", True, detail, subject=f"lab stage: {name}", prev=prev[0])
                env["action"] = name
                fh.write(json.dumps({"ts": datetime.now().astimezone().isoformat(), "run_id": lab_id, "agent": "lab",
                                     **env, **rec}, default=str) + "\n")
            prev[0] = "lab"
            self.bus.publish("lab_step", json.loads(json.dumps(rec, default=str)))

        def work():
            try:
                from app.fixtures import maybe_sample
                from app.lab.experiment import run_experiment
                if self.run_sql is None:
                    from app.lab.offline import offline_run_sql
                    sql, llm_json = offline_run_sql, None  # offline: rule-based hypothesis, generator history
                else:
                    from app import models  # not runtime.chat_json: its one breaker for all roles opens after two
                    sql, llm_json = self.run_sql, maybe_sample(models.chat_json, "json")  # failed incident calls
                self.bus.publish("lab_status", {"status": "running", "lab_id": lab_id})
                started = time.perf_counter()
                loop = self.lab_loop.summary()  # an applied change's measured result goes back to the model too
                on_floor = (loop.get("applied") or {}).get("lab_id")
                tried = [{"policy": r["policy"], "effect_pct": r["effect_pct"], "status": r["status"],
                          **({"applied_to_floor": True, "measured_saving_s": loop.get("measured_saving_s"),
                              "predicted_saving_s": loop.get("predicted_saving_s"), "measured_incidents": loop.get("n")}
                             if r["lab_id"] == on_floor else {})} for r in self.lab_runs]
                result = run_experiment(sql, llm_json, self.grid, on_step=on_step, tried=tried)  # each run builds on the last
                self.last_lab = {**result, "lab_id": lab_id, "trace": f"logs/{trace.name}",
                                 "seconds": round(time.perf_counter() - started, 2), "at": datetime.now().astimezone().isoformat()}
                h = result["hypothesis"]
                self.lab_runs = (self.lab_runs + [{"lab_id": lab_id, "at": self.last_lab["at"], "source": result["source"],
                                                   "model": result.get("model"), "hypothesis": h["hypothesis"],
                                                   "policy": h["policy"], "effect_pct": result["effect_pct"],
                                                   "status": result["status"], "seconds": self.last_lab["seconds"]}])[-20:]
                self._save_lab()
                self.bus.publish("lab", self.last_lab)
            except Exception as err:  # noqa: BLE001
                self.bus.publish("lab", {"error": f"{type(err).__name__}: {err}"[:300]})
            finally:
                self.lab_running = False
                if trace.exists():
                    fill_targets(trace)
        threading.Thread(target=work, daemon=True).start()

    def ask(self, req: dict) -> str:
        """Run the investigator team in the background; steps stream as 'ask_step', the answer as 'ask_result'."""
        question = str(req.get("question") or "").strip()
        if not 5 <= len(question) <= 400:
            raise ValueError("ask a question of 5 to 400 characters")
        if self.asking:
            raise RuntimeError("an investigation is already running")
        self.asking = True
        ask_id = f"ask-{int(time.time())}-{os.urandom(2).hex()}"

        def work():
            from app.fixtures import maybe_sample, sample_mode
            from app import runtime
            from app.investigate.investigator import Investigator
            from app.investigate.sources import sql_runner
            try:
                runner = sql_runner("true") if self.offline and not self.replay else sql_runner(sample_mode(), self.run_sql)
                inv = Investigator(runner, maybe_sample(runtime.chat_json, "json"), ask_id=ask_id, memory=self.memory, sample=sample_mode(),
                                   plant=self.plant, sim=self.sim, history=self.recent_asks(), live=self.live_snapshot,
                                   on_step=lambda aid, st: self.bus.publish("ask_step", {"ask_id": aid, **st}))
                self.last_ask = self.asks.add(inv.run(question))
                self.bus.publish("ask_result", self.last_ask)
            except Exception as err:  # noqa: BLE001 - report to the UI and keep serving
                self.bus.publish("error", {"incident": ask_id, "error": f"{type(err).__name__}: {err}"[:300]})
            finally:
                self.asking = False

        threading.Thread(target=work, daemon=True).start()
        return ask_id

    def nemotron(self, hours: float | None = 24) -> dict:
        """The Nemotron scorecard (app/scorecard.py) for the last `hours` (None: everything): calls per role from
        logs/llm.jsonl, decisions from the incidents finished in the period, receipts signed in it."""
        from app.scorecard import scorecard
        now, cache = time.monotonic(), getattr(self, "_receipts", {})
        if now - cache.get(hours, (-1e9, None))[0] > 30:  # verifying every signature is a file scan: 30 s cache
            try:
                from app.receipts import verify_all
                r = verify_all(since_ms=int((time.time() - hours * 3600) * 1000) if hours else None)
                cache[hours] = (now, {"verified": r["verified"], "total": r["total"], "keys": r["keys"]})
            except Exception:  # noqa: BLE001
                cache[hours] = (now, {})
            self._receipts = cache
        traces = list(self.traces.values())
        if hours:
            since = datetime.now().astimezone() - timedelta(hours=hours)
            traces = [t for iid, t in self.traces.items() if _finished_after(self.trace_meta.get(iid), since)]
        return scorecard(traces, cache[hours][1], hours=hours)

    def _save_lab(self) -> None:
        if self.lab_file is None:
            return
        try:
            with self.lab_loop.lock:
                st = {"runs": self.lab_runs, "applied": self.lab_loop.applied, "measured": self.lab_loop.measured}
            tmp = self.lab_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, default=str))
            tmp.replace(self.lab_file)
        except OSError as err:
            print(f"[WARN] could not save the Lab state: {err}", flush=True)

    def _measure(self, trace) -> None:
        try:
            if self.lab_loop.measure(trace, self.grid) is not None:
                self._save_lab()
                self.bus.publish("lab_loop", self.lab_loop.summary())
        except Exception as err:  # noqa: BLE001 - measuring must never break incident handling
            print(f"[WARN] lab loop measure failed: {err}", flush=True)

    def lab_apply(self) -> dict:
        out = self.lab_loop.apply(self.last_lab)
        self._save_lab()
        self.bus.publish("lab_loop", out)
        return out

    def lab_undo(self) -> dict:
        out = self.lab_loop.undo()
        self._save_lab()
        self.bus.publish("lab_loop", out)
        return out

    def lab_write(self) -> dict:
        from app.lab.experiment import experiment_row, write_zetaris
        if not getattr(self, "last_lab", None):
            raise KeyError("run the lab first")
        if self.run_sql is None:
            raise RuntimeError("offline mode: writing needs Zetaris")
        from app.runtime import run_write  # agents read on the read-only login; this one write uses the operator's
        exp_id = write_zetaris(experiment_row(self.last_lab), run_write)
        self.bus.publish("lab_written", {"experiment_id": exp_id})
        return {"experiment_id": exp_id, "target": "zetaris sf_ops.experiments"}

    def health(self) -> dict:
        if self.offline and not self.degraded:
            from app.agents.health import check_health
            from app.cache import stats as cache_stats
            note = "recorded Nemotron replies + rule-based planner" if self.replay else "offline plant"
            out = check_health({}, {}, self.sim) | {
                "mode": "offline",
                "zetaris": {"status": "offline", "note": "not used: offline plant built from data/seed"},
                "nightshift": {"status": "offline", "note": note}, "caches": cache_stats()}
        else:
            from app.runtime import health
            out = health(self.sim)
        if self.degraded:
            out["degraded"] = self.degraded
        out["plant"] = {"version": getattr(self.plant, "version", None), "loaded_at": getattr(self.plant, "loaded_at", None),
                        "stale": self.plant_stale}
        out["invariants"] = self.invariants.summary()
        try:
            from app.receipts import verify_all
            r = verify_all()
            out["receipts"] = {"verified": r["verified"], "total": r["total"], "keys": r["keys"]}
        except Exception:  # noqa: BLE001
            out["receipts"] = None
        v = self.journal.verify()
        out["journal"] = {"events": v["events"], "chain_ok": v["ok"], "signed": v.get("signed", False),
                          "problem": v["reason"], "replicated_to_postgres": self.journal.replicated,
                          "recovered_interrupted": [i["incident"] for i in self.recovered["interrupted"]]}
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body, ctype="application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):  # noqa: N802
            self.query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.path = self.path.split("?", 1)[0]  # routes match without the query string (e.g. /?shot)
            if self.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, app.snapshot())  # lock-free read: the loop may hold the lock during LLM calls
            if self.path == "/api/powered":
                from app.powered import powered
                return self._send(200, powered(sample_mode=app.replay))
            if self.path == "/api/shift":
                return self._send(200, app.shift_state())
            if self.path == "/api/floor":
                return self._send(200, app.floor_state())
            if self.path == "/api/incidents":
                return self._send(200, app.incidents())
            if self.path.startswith("/api/incident/"):
                try:
                    return self._send(200, app.incident_history(urllib.parse.unquote(self.path.split("/api/incident/", 1)[1])))
                except KeyError:
                    return self._send(404, {"error": "unknown incident"})
            if self.path.startswith("/api/trace/"):
                try:
                    return self._send(200, app.trace_of(urllib.parse.unquote(self.path.split("/api/trace/", 1)[1])))
                except KeyError as err:
                    return self._send(404, {"error": str(err)})
            if self.path == "/api/models":
                from app.models import mapping
                return self._send(200, mapping())
            if self.path == "/api/ask_last":
                return self._send(200, app.last_ask or {})
            if self.path == "/api/asks":
                return self._send(200, app.asks.listing())
            if self.path.startswith("/api/ask/"):
                rec = app.asks.get(urllib.parse.unquote(self.path.split("/api/ask/", 1)[1]))
                return self._send(200, rec) if rec else self._send(404, {"error": "no such answer"})
            if self.path.startswith("/api/nemotron"):
                try:
                    hours = float(self.query.get("hours", ["24"])[0])
                except ValueError:
                    hours = 24.0
                return self._send(200, app.nemotron(hours or None))
            if self.path.startswith("/api/verify/"):
                from app.verify import verify
                iid = urllib.parse.unquote(self.path.split("/api/verify/", 1)[1])
                t = app.traces.get(iid)
                if t is None:
                    return self._send(404, {"error": "unknown or still running incident"})
                with app.lock:
                    return self._send(200, verify(t, app.plant, app.sim, app.journal, app.autonomy))
            if self.path == "/api/vda5050":
                return self._send(200, {"standard": "VDA 5050 v2.0", "orders": list(app.vda.orders)[-20:][::-1],
                                        "states": app.vda.states(app.sim, app.plant)})
            if self.path == "/api/lab_loop":
                return self._send(200, app.lab_loop.summary())
            if self.path == "/api/lab_runs":
                return self._send(200, app.lab_runs[::-1])
            if self.path == "/api/lab_last":
                return self._send(200, app.last_lab or {})
            if self.path == "/api/last":
                return self._send(200, app.last())
            if self.path == "/api/knowledge":
                return self._send(200, app.knowledge())
            if self.path == "/health":
                return self._send(200, app.health())
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = app.bus.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                        except queue.Empty:
                            msg = ": keep-alive\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    app.bus.unsubscribe(q)
                return None
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            try:
                body = self._json() if self.path in ("/run", "/api/run") else None
                # the JSON contract ({"input": {...}}, as in input_examples/) runs synchronously on either path, which is
                # what the hackathon's judge checklist curls; a flat {"kind": ...} on /run is the UI's live-floor report
                if self.path == "/api/run" or (self.path == "/run" and isinstance(body, dict) and "input" in body):
                    from app.api import run_request  # app/api.py; independent of the UI sim
                    result = run_request(body, echo=True)
                    return self._send(200 if result["status"] == "success" else 422, result)
                if self.path == "/run":
                    return self._send(202, {"incident_id": app.run(body)})
                if self.path == "/approve":
                    return self._send(200, app.approve(self._json()))
                if self.path == "/autonomy":
                    mode = self._json().get("mode")
                    if mode not in ("auto", "supervised", "advisory"):
                        raise ValueError("mode must be auto, supervised or advisory")
                    app.autonomy = mode
                    app.bus.publish("autonomy", {"mode": mode})
                    return self._send(200, {"autonomy": mode})
                if self.path == "/shift/start":
                    return self._send(202, app.start_shift(self._json()))
                if self.path == "/shift/stop":
                    return self._send(200, app.stop_shift())
                if self.path == "/ask":
                    return self._send(202, {"ask_id": app.ask(self._json())})
                if self.path == "/lab/run":
                    app.lab()  # a second click while one runs: RuntimeError -> 409
                    return self._send(202, {"status": "running"})
                if self.path == "/lab/write":
                    return self._send(200, app.lab_write())
                if self.path == "/lab/apply":
                    return self._send(200, app.lab_apply())
                if self.path == "/lab/undo":
                    return self._send(200, app.lab_undo())
            except DuplicateIncident as err:
                return self._send(409, {"error": str(err), "existing": err.existing})
            except (KeyError, ValueError, RuntimeError) as err:
                return self._send(400, {"error": str(err)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = 8000, offline: bool = False, host: str = "127.0.0.1", always_on: bool = False) -> None:
    from app.fixtures import sample_mode
    replay = sample_mode() == "true"
    app = App(offline=offline or replay, replay=replay)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as err:
        if err.errno != 98:
            raise
        raise SystemExit(f"Port {port} is already in use by another program (on WSL this can be a Windows app). "
                         f"Start on another port, e.g.  python -m app.server --port {port + 1}")
    print(f"Shop-Floor OS on http://{host}:{port} ({'replay' if replay else 'offline' if app.offline else 'live'})")
    if always_on:  # the Sentinel starts watching with the server and keeps going until someone presses Stop
        app.start_shift({"minutes": 8, "seed": 7, "rate": 2.5, "workers": 2, "continuous": True})
        print("Sentinel always on: shifts follow each other until you press Stop")
    server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--offline", action="store_true", help="no Zetaris: offline plant (use with SAMPLE_MODE)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    ap.add_argument("--always-on", action="store_true", default=os.environ.get("ALWAYS_ON", "").lower() in ("1", "true"),
                    help="start the Sentinel with the server and keep watching, shift after shift, until Stop")
    a = ap.parse_args()
    serve(a.port, a.offline, a.host, a.always_on)
