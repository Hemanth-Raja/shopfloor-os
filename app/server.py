"""Minimal web server, standard library only (http.server). No framework.

    python -m app.server [--port 8000] [--offline]

    GET  /             web/index.html
    GET  /api/state    grid, agents, geofence, last decision
    GET  /events       Server-Sent Events: trace steps, sim ticks, decisions, approvals
    GET  /health       health agent report (circuit breakers, floor vitals)
    POST /run          {"kind", "station", "agv", "false_alarm", "stall_after", "seed"} -> runs the agent loop
    POST /approve      {"incident_id", "approve": true|false, "by"} -> a human judges the decision

Humans judge: an ACT plan is already executing, and rejecting it stops the agents involved. ESCALATE,
SAFE_HOLD and VERIFY wait for a person. Approving one executes its plan, and every judgement is logged
to logs/approvals.jsonl.
"""

from __future__ import annotations

import argparse
import json
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
        out = {"tick": self.sim.tick, "agents": agents, "geofence": sorted(self.sim.geofence),
               "collisions": len(self.sim.collisions)}
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
            self.busy = True
            if req.get("seed") is not None or self.replay:
                self.sim = Sim.from_plant(self.plant, self.grid, seed=int(req.get("seed") or 42), shift="B")
            agv = f"agv:plant:{req['agv']}" if req.get("agv") else None
            inc = self.sim.inject(req["kind"], self.plant, station_id=req.get("station"), agv_id=agv,
                                  false_alarm=bool(req.get("false_alarm")))
        self.bus.publish("incident", inc.model_dump(exclude={"truth_false_alarm"}))
        threading.Thread(target=self._run, args=(inc, req), daemon=True).start()
        return inc.id

    def _run(self, inc, req) -> None:
        try:
            llm_json, llm_text = self.llm()
            orch = Orchestrator(self.plant, self.sim, llm_json, llm_text, memory=self.memory,
                                on_step=lambda iid, st: self.bus.publish("step", {"incident": iid, **st.model_dump()}))
            stall = (0, int(req["stall_after"])) if req.get("stall_after") not in (None, "") else None
            with self.lock:
                trace = orch.handle(inc, stall=stall)
            self.traces[inc.id] = trace
            self.bus.publish("decision", {"incident": inc.id, "decision": trace.decision.model_dump() if trace.decision else None,
                                          "plan": trace.plan.model_dump() if trace.plan else None,
                                          "critic_rejections": trace.critic_rejections, "replans": trace.replans,
                                          "safe_mode": trace.safe_mode,
                                          "needs_approval": bool(trace.decision and trace.decision.decision != "ACT")})
        except Exception as err:  # noqa: BLE001 - report to the UI and keep serving
            self.bus.publish("error", {"incident": inc.id, "error": f"{type(err).__name__}: {err}"[:300]})
        finally:
            self.busy = False

    def approve(self, req: dict) -> dict:
        trace = self.traces.get(req.get("incident_id"))
        if trace is None:
            raise KeyError("unknown incident")
        approved = bool(req.get("approve"))
        action = "none"
        with self.lock:
            if approved and trace.plan and trace.decision and trace.decision.decision != "ACT":
                execute(trace.plan, trace.incident, self.plant, self.sim)
                action = "plan executed after human approval"
            elif not approved and trace.plan:
                for aid in list(trace.plan.assignee_ids) + [r.agv_id for r in trace.plan.agv_routes]:
                    if aid in self.sim.agents and self.sim.agents[aid].status == "moving":
                        self.sim.stop(aid)
                action = "agents stopped after human rejection"
        record = {"ts": datetime.now().astimezone().isoformat(), "incident": trace.incident.id, "approved": approved,
                  "by": req.get("by", "supervisor"), "decision": trace.decision.decision if trace.decision else None,
                  "action": action}
        APPROVALS.parent.mkdir(parents=True, exist_ok=True)
        with APPROVALS.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        self.bus.publish("approval", record)
        return record

    def health(self) -> dict:
        if self.offline:
            from app.agents.health import check_health
            return check_health({}, {}, self.sim) | {"mode": "offline"}
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
            if self.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, app.snapshot())  # lock-free read: the loop may hold the lock during LLM calls
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
            except (KeyError, ValueError, RuntimeError) as err:
                return self._send(400, {"error": str(err)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = 8000, offline: bool = False, host: str = "127.0.0.1") -> None:
    from app.fixtures import sample_mode
    replay = sample_mode() == "true"
    app = App(offline=offline or replay, replay=replay)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Shop-Floor OS on http://{host}:{port} ({'replay' if replay else 'offline' if app.offline else 'live'})")
    server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--offline", action="store_true", help="no Zetaris: offline plant (use with SAMPLE_MODE)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    a = ap.parse_args()
    serve(a.port, a.offline, a.host)
