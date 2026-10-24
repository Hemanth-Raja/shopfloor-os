"""Ask the floor: a team of agents that investigates a question over the plant data and the agents' own logs.

    coordinator (Nemotron) -> sql agents (Nemotron + SQL guard + Zetaris, one retry) + log agent (rules)
        -> analyst (Nemotron) -> critic (rules: every claim must be backed by its cited evidence) -> answer

Every step is streamed (on_step) and written to logs/trace-<ask id>.jsonl. Model calls go through app/llm.py
(via the circuit breakers), data through Zetaris. Without Nightshift, rule-based stand-ins keep it working.
"""

from __future__ import annotations

import hashlib
import os
import json
import re
import threading
from itertools import combinations
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.investigate.catalog import AGENT_CATALOG, AGENT_NOTES, CATALOG, NOTES, about_agents
from app.investigate.sqlguard import GuardError, check

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
FIXTURES = LOGS / "fixtures"
MAX_TASKS = 4
PREVIEW_ROWS = 15
TIMEOUT_S = 90
ANALYST_TIMEOUT_S = 180


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubTask(Strict):
    kind: Literal["sql", "logs"]
    ask: str = Field(min_length=5, max_length=300)


class Decomposition(Strict):
    tasks: list[SubTask] = Field(min_length=1, max_length=MAX_TASKS)


class SqlDraft(Strict):
    sql: str = Field(min_length=10, max_length=2000)
    columns: list[str] = Field(min_length=1, max_length=20)
    purpose: str = Field(min_length=3, max_length=200)


class Finding(Strict):
    claim: str = Field(min_length=5, max_length=800)
    evidence: list[str] = Field(min_length=1)


class ClaimVerdict(Strict):
    index: int
    supported: bool
    reason: str = Field(max_length=300)


class ClaimCheck(Strict):
    verdicts: list[ClaimVerdict] = Field(max_length=12)


class AnswerDraft(Strict):
    summary: str = Field(min_length=5, max_length=1500)
    findings: list[Finding] = Field(max_length=10)
    recommendation: str = Field(min_length=3, max_length=800)


def catalog_text(question: str = "") -> str:
    text = "\n".join(f"{t}({', '.join(cols)})" for t, cols in CATALOG.items()) + "\n" + NOTES
    if about_agents(question):  # the agents' own history, only when the question is about them
        text += "\n" + "\n".join(f"{t}({', '.join(cols)})" for t, cols in AGENT_CATALOG.items()) + "\n" + AGENT_NOTES
    return text


# ---------------------------------------------------------------- rule-based stand-ins (no model)

TEMPLATES = {
    "stations": ("SELECT station_id, count(*) AS incidents, round(sum(downtime_min), 1) AS downtime_min, "
                 "sum(cars_lost) AS cars_lost FROM sf_ops.incidents WHERE station_id IS NOT NULL "
                 "GROUP BY station_id ORDER BY downtime_min DESC LIMIT 10",
                 ["station_id", "incidents", "downtime_min", "cars_lost"], "downtime by station"),
    "station_causes": ("SELECT kind, cause, count(*) AS n, round(sum(downtime_min), 1) AS downtime_min "
                       "FROM sf_ops.incidents WHERE station_id = '{station}' GROUP BY kind, cause "
                       "ORDER BY downtime_min DESC LIMIT 10",
                       ["kind", "cause", "n", "downtime_min"], "what stops station {station}"),
    "agv_faults": ("SELECT i.agv_id, i.shift_code, count(*) AS faults, a.battery_health_pct, a.last_service_date "
                   "FROM sf_ops.incidents i JOIN sf_fleet.agvs a ON a.agv_id = i.agv_id WHERE i.kind = 'agv_fault' "
                   "GROUP BY i.agv_id, i.shift_code, a.battery_health_pct, a.last_service_date "
                   "ORDER BY faults DESC LIMIT 12",
                   ["agv_id", "shift_code", "faults", "battery_health_pct", "last_service_date"], "AGV faults by shift"),
    "overheat": ("SELECT CASE WHEN outdoor_temp_c >= 25 THEN 'hot (>=25C)' ELSE 'cooler' END AS weather, "
                 "count(*) AS alarms, sum(CASE WHEN false_alarm THEN 1 ELSE 0 END) AS false_alarms "
                 "FROM sf_ops.incidents WHERE kind = 'machine_overheat' GROUP BY 1",
                 ["weather", "alarms", "false_alarms"], "overheat alarms against outdoor temperature"),
    "recent": ("SELECT incident_id, ts, kind, station_id, severity, downtime_min FROM sf_ops.incidents "
               "ORDER BY ts DESC LIMIT 10", ["incident_id", "ts", "kind", "station_id", "severity", "downtime_min"],
               "most recent incidents"),
}


def rule_tasks(question: str) -> list[dict]:
    q = question.lower()
    station = next((s for s in re.findall(r"\b([btpmfe]\d0)\b", q)), None)
    tasks = []
    if station:
        tasks += [{"template": "station_causes", "station": station.upper()}, {"template": "stations"}]
    if "agv" in q or "robot" in q or "reliab" in q:
        tasks.append({"template": "agv_faults"})
    if "overheat" in q or "alarm" in q or "temperature" in q:
        tasks.append({"template": "overheat"})
    if not tasks:
        tasks = [{"template": "stations"}, {"template": "recent"}]
    return tasks[:MAX_TASKS - 1] + [{"logs": True}]


def tidy(v):
    """Round long floats (Zetaris returns some as strings) so the analyst copies short numbers."""
    if isinstance(v, str) and re.fullmatch(r"-?\d+\.\d{3,}", v):
        v = float(v)
    return round(v, 2) if isinstance(v, float) else v


def supported_numbers(evidence: list[dict]) -> list[float]:
    """Every number in the evidence, plus row counts and totals of up to three values from one result column."""
    text = json.dumps(evidence, default=str)
    values = {float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)}
    for e in evidence:
        for col in zip(*(e.get("preview") or [])):
            nums = [float(v) for v in col if (isinstance(v, (int, float)) and not isinstance(v, bool))
                    or (isinstance(v, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", v))]
            values.add(sum(nums))
            for k in (2, 3):
                values.update(sum(c) for c in combinations(nums, k))
    return sorted(values)


# ---------------------------------------------------------------- the team

class Investigator:
    def __init__(self, run_sql: Callable, llm_json: Callable | None, on_step: Callable | None = None,
                 ask_id: str | None = None, memory=None, sample: str = "false"):
        self.run_sql, self.llm_json, self.on_step = run_sql, llm_json, on_step
        self.question = ""
        self.ask_id = ask_id or f"ask-{int(time.time())}-{os.urandom(2).hex()}"
        self.memory = memory
        self.sample = sample  # SAMPLE_MODE: the log agent's evidence is recorded/replayed too (logs differ per machine)
        self.trace_path = LOGS / f"trace-{self.ask_id}.jsonl"
        self.models: set[str] = set()
        self._lock = threading.Lock()
        self.analyst_error: str | None = None
        self.role_calls: dict[str | None, dict] = {}

    def _step(self, name: str, started: float, ok: bool, **detail) -> None:
        rec = {"step": name, "ok": ok, "ms": round((time.perf_counter() - started) * 1000, 2), "detail": detail}
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().astimezone().isoformat(), "run_id": self.ask_id,
                                 "incident": self.ask_id, "agent": name, **rec}, default=str) + "\n")
        if self.on_step:
            self.on_step(self.ask_id, rec)

    def _llm(self, schema, messages, lane="deep", timeout_s=TIMEOUT_S, role=None, sink: dict | None = None):
        if self.llm_json is None:
            raise RuntimeError("no model configured")
        reply = self.llm_json(lane, messages, schema, timeout_s=timeout_s, role=role)
        self.models.add(str(getattr(reply, "model", "?")))
        from app.models import call_info
        info = call_info(reply)
        self.role_calls[role] = info
        if sink is not None:
            sink["model_call"] = info
        return reply.data if hasattr(reply, "data") else reply

    # -- coordinator
    def plan(self, question: str) -> tuple[list[dict], str]:
        try:
            d = self._llm(Decomposition, role="coordinator", messages=[
                {"role": "system", "content": "You coordinate a team investigating a car final-assembly hall. Split the "
                 "question into 1-4 sub-tasks. Use kind 'sql' for questions the plant data can answer (the SQL agent "
                 "queries these tables through Zetaris) and at most one kind 'logs' task for what the agents themselves "
                 "did (their traces, model calls, human approvals, learned lessons). Make each ask specific. The sub-tasks run "
                 "in parallel and cannot see each other's results, so never assume an answer (such as a particular AGV "
                 "or station) that the question does not name, and never refer to another task ('the AGV from task 1'): "
                 "each ask must stand alone, so rank or group instead (e.g. faults per AGV per shift).\n"
                 + catalog_text(question)},
                {"role": "user", "content": question}])
            tasks = [{"kind": t.kind, "ask": t.ask} for t in d.tasks]
            if not any(t["kind"] == "logs" for t in tasks):
                tasks.append({"kind": "logs", "ask": f"What did our agents do or learn that relates to: {question}"})
            return tasks[:MAX_TASKS + 1], "nemotron"
        except Exception:  # noqa: BLE001 - rule-based coordinator when Nightshift is unavailable
            tasks = []
            for t in rule_tasks(question):
                if t.get("logs"):
                    tasks.append({"kind": "logs", "ask": f"What did our agents do or learn that relates to: {question}"})
                else:
                    sql, cols, purpose = TEMPLATES[t["template"]]
                    tasks.append({"kind": "sql", "ask": purpose.format(**t), "template": t})
            return tasks, "rule-based"

    # -- SQL agent
    def sql_task(self, task: dict, n: int, earlier: list[dict] | None = None) -> dict:
        ev = {"id": f"E{n}", "type": "sql", "task": task["ask"]}
        context = [{"task": e["task"], "columns": e.get("columns"), "rows": (e.get("preview") or [])[:5]}
                   for e in earlier or [] if e.get("ok")]
        if "template" in task:
            sql, cols, purpose = TEMPLATES[task["template"]["template"]]
            drafts = [SqlDraft(sql=sql.format(**task["template"]), columns=cols, purpose=purpose.format(**task["template"]))]
        else:
            drafts = []
        error = None
        for attempt in (1, 2):
            if attempt > len(drafts):
                try:
                    msgs = [{"role": "system", "content": "You are the SQL agent. Write ONE read-only Zetaris Lightning "
                             "(Spark SQL) SELECT that answers the task. Qualify every table as sf_<db>.<table> and use only "
                             "these tables and columns. Aggregate: use GROUP BY with count/sum/avg so the answer fits in "
                             "at most 20 rows, ordered by the most important measure; never return raw incident rows. "
                             "Round averages to 1 decimal. Return the result column names in order.\n" + catalog_text(self.question)},
                            {"role": "user", "content": task["ask"]}]
                    if context:  # this task builds on another one: give it those rows
                        msgs.append({"role": "user", "content": "Results of the task(s) this one depends on: "
                                     + json.dumps(context, default=str)})
                    if error:
                        msgs.append({"role": "user", "content": f"Your previous query failed: {error}. Fix it."})
                    drafts.append(self._llm(SqlDraft, msgs, lane="fast", role="sql_agent", sink=ev))
                except Exception as err:  # noqa: BLE001
                    ev.update(ok=False, error=f"SQL agent unavailable: {str(err)[:160]}")
                    return ev
            draft = drafts[attempt - 1]
            try:
                sql = check(draft.sql)
                started = time.perf_counter()
                rows = self.run_sql(sql)
                preview = [[tidy(v) for v in r] for r in rows[:PREVIEW_ROWS]]
                ev.update(ok=True, sql=sql, columns=draft.columns, purpose=draft.purpose, rows=len(rows),
                          preview=preview, attempt=attempt,
                          query_ms=round((time.perf_counter() - started) * 1000, 1))
                return ev
            except GuardError as err:
                error = f"refused by the SQL guard: {err}"
            except Exception as err:  # noqa: BLE001 - SQL error: one retry with the message
                error = f"Zetaris error: {str(err)[:300]}"
            ev.update(ok=False, error=error, sql=draft.sql, attempt=attempt)
        return ev

    # -- log agent (rules only)
    def logs_task(self, question: str, n: int) -> dict:
        fixture = FIXTURES / f"logs-{hashlib.sha256(question.strip().lower().encode()).hexdigest()[:20]}.json"
        if self.sample == "true" and fixture.exists():
            return {**json.loads(fixture.read_text()), "id": f"E{n}"}
        ev = self._search_logs(question, n)
        if self.sample == "record":
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps(ev, indent=1, default=str))
        return ev

    def _search_logs(self, question: str, n: int) -> dict:
        keys = {k.upper() for k in re.findall(r"\b(?:[btpmfe]\d0|agv-\d|w-\d\d|m-0\d)\b", question.lower())}
        keys |= {w for w in ("part_shortage", "agv_fault", "overheat", "spill") if w.split("_")[0] in question.lower()}
        traces = sorted(LOGS.glob("trace-*.jsonl"), key=lambda p: p.stat().st_mtime)[-60:]
        decisions, rejections, replans, hits = {}, {}, 0, []
        for path in traces:
            if path.name.startswith("trace-ask-"):
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if keys and not any(k in line for k in keys):
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = r.get("detail", {})
                if r.get("step") == "restraint":
                    decisions[d.get("decision")] = decisions.get(d.get("decision"), 0) + 1
                if r.get("step") == "critic" and not r.get("ok"):
                    for v in d.get("violations", []):
                        rejections[v["rule_id"]] = rejections.get(v["rule_id"], 0) + 1
                if r.get("step") == "verifier" and not r.get("ok"):
                    replans += 1
                if len(hits) < 6 and r.get("step") in ("critic", "restraint", "verifier"):
                    hits.append(f"{path.name}:{i} {r.get('step')} ok={r.get('ok')}")
        calls, latency, models = 0, 0.0, set()
        llm = LOGS / "llm.jsonl"
        if llm.exists():
            for line in llm.read_text().splitlines()[-200:]:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                calls += 1
                latency += float(r.get("latency_s") or 0)
                models.add(str(r.get("answering_model")))
        lessons = []
        if self.memory is not None:
            for m in sorted(self.memory.hmem.store.all(), key=lambda m: -m["confidence"])[:5]:
                lessons.append(f"{m['content'][:120]} (confidence {m['confidence']:.2f}, source {m['source']})")
        summary = {"incidents_matched_in_traces": sum(decisions.values()), "decisions": decisions,
                   "critic_rejections_by_rule": rejections, "verifier_replans": replans,
                   "nemotron_calls_recent": calls, "avg_latency_s": round(latency / calls, 1) if calls else None,
                   "models": sorted(models), "h_mem_lessons": lessons}
        return {"id": f"E{n}", "type": "logs", "task": "agent traces, model calls and H-MEM lessons",
                "ok": True, "keys": sorted(keys), "summary": summary, "refs": hits}

    # -- analyst
    def analyse(self, question: str, evidence: list[dict]) -> tuple[AnswerDraft, str]:
        shown = [{k: e.get(k) for k in ("id", "type", "task", "purpose", "columns", "preview", "rows", "summary", "error")
                  if e.get(k) is not None} for e in evidence]
        messages = [
            {"role": "system", "content": "You are the analyst. Answer the question for a plant supervisor using ONLY "
             "the evidence. Every finding must cite evidence ids such as E1. Copy numbers exactly as they appear in "
             "the evidence (a total of up to three values from one column is fine; no other arithmetic). If the "
             "evidence is insufficient, say so. At most 5 findings, one sentence each. Give one concrete "
             "recommendation."},
            {"role": "user", "content": json.dumps({"question": question, "evidence": shown}, default=str)}]
        for attempt in (1, 2):  # one retry: the model sometimes answers in prose instead of JSON
            try:
                return self._llm(AnswerDraft, messages if attempt == 1 else messages + [
                    {"role": "user", "content": "Reply with the JSON object only, matching the schema."}],
                    timeout_s=ANALYST_TIMEOUT_S, role="analyst"), "nemotron"
            except Exception as err:  # noqa: BLE001
                self.analyst_error = f"{type(err).__name__}: {str(err)[:200]}"
        # rule-based analyst (no model): describe the top rows
        findings = []
        for e in evidence:
            if e["type"] == "sql" and e.get("ok") and e.get("preview"):
                top = e["preview"][0]
                pairs = ", ".join(f"{c}={v}" for c, v in zip(e["columns"], top))
                findings.append(Finding(claim=f"For {e['purpose']}, the top row is {pairs}.", evidence=[e["id"]]))
        logs = next((e for e in evidence if e["type"] == "logs"), None)
        if logs and logs["summary"]["incidents_matched_in_traces"]:
            s = logs["summary"]
            findings.append(Finding(claim=f"Our agents handled {s['incidents_matched_in_traces']} related incident(s) "
                                          f"with decisions {s['decisions']}.", evidence=[logs["id"]]))
        return AnswerDraft(summary=f"Rule-based summary of {len(evidence)} pieces of evidence for: {question}",
                           findings=findings[:8], recommendation="Review the top rows above with the shift supervisor."), "rule-based"

    # -- critic (rules only)
    @staticmethod
    def critic(draft: AnswerDraft, evidence: list[dict]) -> tuple[list[dict], list[dict]]:
        by_id = {e["id"]: e for e in evidence}
        kept, dropped = [], []
        for f in draft.findings:
            missing = [i for i in f.evidence if i not in by_id or not by_id[i].get("ok")]
            if missing or not f.evidence:
                dropped.append({"claim": f.claim, "reason": f"cites evidence that does not exist or failed: {missing}"})
                continue
            values = supported_numbers([by_id[i] for i in f.evidence])
            unsupported = []
            claim = re.sub(r"(?<=\d),(?=\d{3}\b)", "", f.claim)  # 30,055 -> 30055
            for num in re.findall(r"(?<![\w.-])\d+(?:\.\d+)?", claim):  # 24.4C and 7.0% count; AGV-3, E1 don't
                v = float(num)
                if v < 10 and "." not in num:
                    continue  # small counts like "3 stations" are not checked
                if not any(abs(v - x) <= max(0.051, 0.01 * abs(x)) for x in values):
                    unsupported.append(num)
            if unsupported:
                dropped.append({"claim": f.claim, "reason": f"numbers not found in the cited evidence: {unsupported}"})
            else:
                kept.append({"claim": f.claim, "evidence": f.evidence})
        return kept, dropped

    def _call(self, role: str, how: str) -> dict:
        info = self.role_calls.get(role)
        return {"model_call": info} if info and how and how != "rule-based" else {}

    # -- the whole investigation
    def run(self, question: str) -> dict:
        started = time.perf_counter()
        t = time.perf_counter()
        self.question = question
        tasks, how = self.plan(question)
        self._step("coordinator", t, True, question=question, tasks=[{"kind": x["kind"], "ask": x["ask"]} for x in tasks],
                   model=how, **self._call("coordinator", how))
        def work(n_task):
            n, task = n_task
            t = time.perf_counter()
            if task["kind"] == "sql":
                ev = self.sql_task(task, n, [results[d] for d in sorted(deps[n]) if d in results])
                self._step("sql", t, bool(ev.get("ok")), **{k: v for k, v in ev.items() if k != "ok"})
            else:
                ev = self.logs_task(question, n)
                self._step("logs", t, True, **{k: v for k, v in ev.items() if k != "ok"})
            return ev
        numbered = list(enumerate(tasks, 1))
        deps = {n: {int(d) for d in re.findall(r"\btasks?\s*#?(\d)", t["ask"], re.I) if 0 < int(d) < n}
                for n, t in numbered}
        results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=MAX_TASKS + 1) as pool:  # independent sub-tasks run at once,
            while len(results) < len(numbered):                     # a task that names another runs after it
                wave = [(n, t) for n, t in numbered if n not in results and deps[n] <= results.keys()]
                wave = wave or [(n, t) for n, t in numbered if n not in results]
                for (n, _), ev in zip(wave, pool.map(work, wave)):
                    results[n] = ev
        evidence = [results[n] for n, _ in numbered]
        t = time.perf_counter()
        draft, how_a = self.analyse(question, evidence)
        self._step("analyst", t, True, summary=draft.summary, findings=len(draft.findings), model=how_a,
                   **self._call("analyst", how_a),
                   **({"fallback_reason": self.analyst_error} if how_a == "rule-based" and self.analyst_error else {}))
        t = time.perf_counter()
        kept, dropped = self.critic(draft, evidence)
        checked_by = ["rules"]
        if kept and self.llm_json is not None:  # second, independent check by a different model: it can only drop
            try:
                verdict = self._llm(ClaimCheck, role="evidence_critic", timeout_s=120, messages=[
                    {"role": "system", "content": "You are an evidence critic. For each claim, say whether the cited "
                     "evidence rows really support it (numbers, ids and the direction of the comparison). Be strict: "
                     "unsupported or over-stated claims get supported=false with a short reason."},
                    {"role": "user", "content": json.dumps({"claims": [{"index": i, **k} for i, k in enumerate(kept)],
                                                            "evidence": [{x: e.get(x) for x in ("id", "columns", "preview",
                                                                                                "summary") if e.get(x)}
                                                                         for e in evidence]}, default=str)}])
                bad = {v.index: v.reason for v in verdict.verdicts if not v.supported and 0 <= v.index < len(kept)}
                dropped += [{"claim": k["claim"], "reason": f"evidence critic model: {bad[i]}"}
                            for i, k in enumerate(kept) if i in bad]
                kept = [k for i, k in enumerate(kept) if i not in bad]
                checked_by.append(self.role_calls.get("evidence_critic", {}).get("answering") or "model")
            except Exception as err:  # noqa: BLE001 - the rule check stands on its own
                checked_by.append(f"model check skipped ({type(err).__name__})")
        self._step("critic", t, not dropped, kept=len(kept), dropped=dropped, checked_by=checked_by,
                   **self._call("evidence_critic", "model" if len(checked_by) > 1 and "skipped" not in checked_by[-1] else ""))
        return {"ask_id": self.ask_id, "question": question, "summary": draft.summary, "findings": kept,
                "dropped": dropped, "recommendation": draft.recommendation, "evidence": evidence,
                "models": sorted(self.models) or ["rule-based"], "coordinator": how, "analyst": how_a,
                "log_file": f"logs/{self.trace_path.name}", "execution_time_seconds": round(time.perf_counter() - started, 2)}
