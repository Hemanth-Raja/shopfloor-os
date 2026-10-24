"""Minimal web server, standard library only (http.server). No framework.

    python -m app.server [--port 8000] [--offline]

    GET  /             web/index.html
    GET  /api/state    grid, agents, geofence, last decision
    GET  /events       Server-Sent Events: trace steps, sim ticks, decisions, approvals
    GET  /health       health agent report (circuit breakers, floor vitals)
    GET  /api/knowledge what the agents know: World Model entities and checks, H-MEM lessons and ledger
    POST /ask          investigator team in the background -> 'ask_step' and 'ask_result' events; GET /api/ask_last
    POST /lab/run      Lab Level 2 experiment in the background -> 'lab' event; POST /lab/write saves it
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
import json
import os
import queue
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.agents.executor import execute
from app.engines.playbooks import PlaybookStore
from app.orchestrator import Orchestrator
from app.sim.grid import Grid
from app.sim.sim import Sim

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
APPROVALS = ROOT / "logs" / "approvals.jsonl"
TICK_INTERVAL_S = 0.5


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


class App:
    def __init__(self, offline: bool = False, replay: bool = False):
        self.bus = Bus()
        self.replay = replay  # SAMPLE_MODE=true: every run starts from the recorded scenario state
        self.lock = threading.RLock()
        self.offline = offline
        if offline:
            from app.sim.offline import offline_plant
            self.plant, self.run_sql = offline_plant(), None
        else:
            from app.engines.plant_world import load_plant_world
            from app.runtime import run_sql
            self.run_sql = run_sql
            self.plant = load_plant_world(run_sql, today=date(2026, 10, 16), at="2026-10-16T14:00:00.000Z")
        self.grid = Grid.load(self.run_sql)
        self.sim = Sim.from_plant(self.plant, self.grid, seed=42, shift="B")
        self.memory = None if replay else PlaybookStore()  # learned lessons would change the replay keys
        self.traces: dict[str, object] = {}
        self.last_lab: dict | None = None
        self.last_ask: dict | None = None
        self.asking = False
        self.autonomy = "auto"
        self.busy = False
        threading.Thread(target=self._ticker, daemon=True).start()

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

    def snapshot(self, full: bool = True) -> dict:
        agents = [{"id": a.id, "kind": a.kind, "x": a.pos[0], "y": a.pos[1], "status": a.status}
                  for a in self.sim.agents.values()]
        last = list(self.traces.values())[-1] if self.traces else None
        involved = sorted(last.dispatch) if last else []
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
        with self.lock:
            if self.busy:
                raise RuntimeError("an incident is already running")
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
            self.busy = True
            try:
                if req.get("seed") is not None or self.replay:
                    self.sim = Sim.from_plant(self.plant, self.grid, seed=int(req.get("seed") or 42), shift="B")
                inc = self.sim.inject(req["kind"], self.plant, station_id=req.get("station"), agv_id=agv,
                                      false_alarm=bool(req.get("false_alarm")), severity=req.get("severity") or None,
                                      note=req.get("note"))
            except Exception:
                self.busy = False
                raise
        self.bus.publish("incident", inc.model_dump(exclude={"truth_false_alarm"}))
        threading.Thread(target=self._run, args=(inc, req), daemon=True).start()
        return inc.id

    def _run(self, inc, req) -> None:
        try:
            llm_json, llm_text = self.llm()
            orch = Orchestrator(self.plant, self.sim, llm_json, llm_text, memory=self.memory, run_id=inc.id,
                                autonomy=req.get("autonomy") or self.autonomy, echo=True,
                                on_step=lambda iid, st: self.bus.publish("step", {"incident": iid, **st.model_dump()}))
            stall = (0, int(req["stall_after"])) if req.get("stall_after") not in (None, "") else None
            with self.lock:
                trace = orch.handle(inc, stall=stall)
            self.traces[inc.id] = trace
            self.bus.publish("decision", {"incident": inc.id, "decision": trace.decision.model_dump() if trace.decision else None,
                                          "plan": trace.plan.model_dump() if trace.plan else None,
                                          "critic_rejections": trace.critic_rejections, "replans": trace.replans,
                                          "safe_mode": trace.safe_mode, "gate": trace.gate,
                                          "gate_reasons": trace.gate_reasons, "autonomy": trace.autonomy})
        except Exception as err:  # noqa: BLE001 - report to the UI and keep serving
            self.bus.publish("error", {"incident": inc.id, "error": f"{type(err).__name__}: {err}"[:300]})
        finally:
            self.busy = False

    def approve(self, req: dict) -> dict:
        """The human gate. Waiting/advisory plans run only on Approve; an auto-executed plan can be rejected,
        which stops its agents and sends them back where they started (roll-back)."""
        from app.orchestrator import executable_plan
        trace = self.traces.get(req.get("incident_id"))
        if trace is None:
            raise KeyError("unknown incident")
        approved = bool(req.get("approve"))
        action = "none"
        with self.lock:
            plan = executable_plan(trace)
            if trace.gate in ("awaiting_approval", "advisory"):
                if approved and plan is not None:
                    for aid in list(plan.assignee_ids) + [r.agv_id for r in plan.agv_routes]:
                        if aid in self.sim.agents:
                            trace.dispatch.setdefault(aid, list(self.sim.agents[aid].pos))
                    execute(plan, trace.incident, self.plant, self.sim)
                    trace.gate, action = "approved", "plan dispatched after human approval"
                else:
                    trace.gate, action = "rejected", "plan dropped by the human; nothing was dispatched"
            elif trace.gate == "auto_executed" and not approved:
                for aid, start in trace.dispatch.items():
                    if aid in self.sim.agents:
                        self.sim.stop(aid)
                        if tuple(start) != self.sim.agents[aid].pos:
                            self.sim.assign(aid, tuple(start))  # roll back: return to where it started
                trace.gate, action = "rolled_back", "auto-executed plan rolled back: agents stopped and sent back"
            elif approved:
                action = "auto-executed plan confirmed by the human"
        self.bus.publish("gate", {"incident": trace.incident.id, "gate": trace.gate})
        record = {"ts": datetime.now().astimezone().isoformat(), "incident": trace.incident.id, "approved": approved,
                  "by": req.get("by", "supervisor"), "decision": trace.decision.decision if trace.decision else None,
                  "gate": trace.gate, "action": action}
        APPROVALS.parent.mkdir(parents=True, exist_ok=True)
        with APPROVALS.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        self.bus.publish("approval", record)
        return record

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
                             "gate_reasons": trace.gate_reasons, "autonomy": trace.autonomy}}

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
        """Run Lab Level 2 in the background and publish the result as a 'lab' event."""
        def work():
            try:
                from app.fixtures import maybe_sample
                from app.lab.experiment import run_experiment
                if self.run_sql is None:
                    from app.lab.offline import offline_run_sql
                    sql, llm_json = offline_run_sql, None  # offline: rule-based hypothesis, generator history
                else:
                    from app import runtime
                    sql, llm_json = self.run_sql, maybe_sample(runtime.chat_json, "json")
                self.bus.publish("lab_status", {"status": "running"})
                result = run_experiment(sql, llm_json, self.grid)
                self.last_lab = result
                self.bus.publish("lab", result)
            except Exception as err:  # noqa: BLE001
                self.bus.publish("lab", {"error": f"{type(err).__name__}: {err}"[:300]})
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
                                   on_step=lambda aid, st: self.bus.publish("ask_step", {"ask_id": aid, **st}))
                self.last_ask = inv.run(question)
                self.bus.publish("ask_result", self.last_ask)
            except Exception as err:  # noqa: BLE001 - report to the UI and keep serving
                self.bus.publish("error", {"incident": ask_id, "error": f"{type(err).__name__}: {err}"[:300]})
            finally:
                self.asking = False

        threading.Thread(target=work, daemon=True).start()
        return ask_id

    def lab_write(self) -> dict:
        from app.lab.experiment import experiment_row, write_zetaris
        if not getattr(self, "last_lab", None):
            raise KeyError("run the lab first")
        if self.run_sql is None:
            raise RuntimeError("offline mode: writing needs Zetaris")
        exp_id = write_zetaris(experiment_row(self.last_lab), self.run_sql)
        self.bus.publish("lab_written", {"experiment_id": exp_id})
        return {"experiment_id": exp_id, "target": "zetaris sf_ops.experiments"}

    def health(self) -> dict:
        if self.offline:
            from app.agents.health import check_health
            note = "recorded Nemotron replies + rule-based planner" if self.replay else "offline plant"
            return check_health({}, {}, self.sim) | {
                "mode": "offline",
                "zetaris": {"status": "offline", "note": "not used: offline plant built from data/seed"},
                "nightshift": {"status": "offline", "note": note}}
        from app.runtime import health
        return health(self.sim)


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
            self.path = self.path.split("?", 1)[0]  # ignore query strings (e.g. /?shot)
            if self.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, app.snapshot())  # lock-free read: the loop may hold the lock during LLM calls
            if self.path == "/api/powered":
                from app.powered import powered
                return self._send(200, powered(sample_mode=app.replay))
            if self.path == "/api/ask_last":
                return self._send(200, app.last_ask or {})
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
                if self.path == "/run":
                    return self._send(202, {"incident_id": app.run(self._json())})
                if self.path == "/approve":
                    return self._send(200, app.approve(self._json()))
                if self.path == "/api/run":  # synchronous JSON contract (app/api.py); independent of the UI sim
                    from app.api import run_request
                    result = run_request(self._json(), echo=True)
                    return self._send(200 if result["status"] == "success" else 422, result)
                if self.path == "/autonomy":
                    mode = self._json().get("mode")
                    if mode not in ("auto", "supervised", "advisory"):
                        raise ValueError("mode must be auto, supervised or advisory")
                    app.autonomy = mode
                    app.bus.publish("autonomy", {"mode": mode})
                    return self._send(200, {"autonomy": mode})
                if self.path == "/ask":
                    return self._send(202, {"ask_id": app.ask(self._json())})
                if self.path == "/lab/run":
                    app.lab()
                    return self._send(202, {"status": "running"})
                if self.path == "/lab/write":
                    return self._send(200, app.lab_write())
            except (KeyError, ValueError, RuntimeError) as err:
                return self._send(400, {"error": str(err)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = 8000, offline: bool = False, host: str = "127.0.0.1") -> None:
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
    server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--offline", action="store_true", help="no Zetaris: offline plant (use with SAMPLE_MODE)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    a = ap.parse_args()
    serve(a.port, a.offline, a.host)
