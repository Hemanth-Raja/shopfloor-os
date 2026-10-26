"""Shift handover on the Meterless Markovian engine: the whole shift journal, read in bounded slices.

A shift writes hundreds of journal lines (queued, started, leases, pre-emptions, approvals, roll-backs). Sending all
of them, plus every earlier answer, to the model at each step would grow the prompt with the shift. Instead the
journal is cut into slices of `SLICE` compact lines; each Markovian chunk reads one slice plus the carried state
(counts, open incidents, risks) and the final reflection writes the handover note for the next shift lead.

Measured, not assumed: for every chunk we record the real prompt size and the size the same step would have had if
it re-sent all earlier slices and answers. Sizes use the engine's characters/4 estimate.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from app.engines.markovian import STATE, ChunkConfig, MarkovianEngine, register_mode, tokens
from app.journal import Journal

LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
SLICE = 40  # journal lines per chunk
SYSTEM = ("You are the shift handover assistant for a car final-assembly hall. You read the agents' journal slice by "
          "slice. Be factual: use only ids and numbers that appear in the journal or the state. No markdown tables.")
GOAL = ("Prepare the handover for the next shift lead: incidents by kind, what was auto-resolved, what still waits for "
        "a person (with incident ids), roll-backs and pre-emptions, people and AGVs that were busiest, and risks to "
        "watch next shift.")
REFLECT = ("Write the handover note (max 180 words): 1) one-line summary with counts, 2) still open / waiting for a "
           "person, with ids, 3) what went wrong or was rolled back, 4) watch next shift. Use only facts from the state "
           "and the notes below.\n\nFinal state:\n{state}\n\nChunk notes:\n{notes}")
# a direct, bounded instruction per chunk: the generic ARCHITECT template ("analyze, plan, start") made the model
# reason at length over every slice
register_mode("HANDOVER",
              "{memory}Goal: {goal}\n\nJournal slice:\n{input}\n\nWrite at most 120 words of notes on this slice (counts "
              "by kind, incidents waiting for a person with ids, roll-backs, pre-emptions). {rules}",
              "Goal (summary): {goal_short}\n\nState so far:\n{carryover}\n\nNext journal slice:\n{input}\n\nWrite at most 120 "
              "words of notes on what this slice adds or changes. {rules}")
SKIP = {"lease", "release"}  # folded into per-resource counts; one line each would drown the slices


def last_shift(events: list[dict], shifts: int = 1) -> list[dict]:
    """Events of the last `shifts` shifts (from a shift_started to the last shift_finished, or the journal's end)."""
    events = [e for e in events if "kind" in e]  # a torn last line (crash mid-write) is skipped
    starts = [i for i, e in enumerate(events) if e["kind"] == "shift_started"]
    if not starts:
        return events
    out = events[starts[-min(shifts, len(starts))]:]
    end = max((i for i, e in enumerate(out) if e["kind"] == "shift_finished"), default=None)
    if end is None or any(e["kind"] == "shift_started" for e in out[end + 1:]):
        return out  # the last shift is still running: hand over everything so far
    return out[:end + 1]


def compact(e: dict) -> str:
    d = dict(e.get("data", {}))
    for k in ("ts", "audit_hash", "prev", "hash"):
        d.pop(k, None)
    return f"#{e.get('seq')} {e['kind']} " + json.dumps(d, separators=(",", ":"), default=str)[:300]


def slices(events: list[dict], size: int = SLICE) -> list[str]:
    usage: dict[str, int] = {}
    lines = []
    for e in events:
        if e["kind"] == "lease":
            usage[e["data"].get("resource", "?")] = usage.get(e["data"].get("resource", "?"), 0) + 1
        if e["kind"] not in SKIP:
            lines.append(compact(e))
    if usage:
        top = sorted(usage.items(), key=lambda kv: -kv[1])[:8]
        lines.append("#- leases per resource this shift: " + ", ".join(f"{r.split(':')[-1]}={n}" for r, n in top))
    return ["\n".join(lines[i:i + size]) for i in range(0, len(lines), size)] or ["(empty journal)"]


def measure(run, inputs: list[str]) -> dict:
    """Real prompt tokens per chunk vs the same step re-sending all earlier inputs and answers (standard)."""
    rows, history = [], 0
    for c, inp in zip(run.chunks, inputs):
        standard = history + c.prompt_tokens
        rows.append({"step": c.id, "markovian_prompt": c.prompt_tokens, "standard_prompt": standard,
                     "carryover": tokens(c.carryover)})
        history += tokens(inp) + c.tokens
    m, s = sum(r["markovian_prompt"] for r in rows), sum(r["standard_prompt"] for r in rows)
    return {"per_step": rows, "markovian_prompt_tokens": m, "standard_prompt_tokens": s,
            "saved_percent": round(100 * (s - m) / s, 1) if s else 0.0,
            "spec_output_efficiency_percent": run.efficiencyPercent}


def handover(chat: Callable, journal_path: Path | None = None, events: list[dict] | None = None, shifts: int = 1,
             on_phase: Callable | None = None, config: ChunkConfig | None = None, log_path=None,
             shift_log_dir: Path | None = LOGS_DIR) -> dict:
    """chat(lane, messages, role=...) -> reply with .text (app.runtime.chat or a stub)."""
    events = last_shift(events if events is not None else Journal(journal_path).events(), shifts)
    inputs = slices(events)

    def gen(role: str, lane: str):
        return lambda prompt, system: chat(lane, [{"role": "system", "content": system},
                                                  {"role": "user", "content": prompt}], role=role).text

    def reflect_prompt(run) -> str:
        notes = "\n".join(f"- chunk {c.id}: {c.content[:400]}" for c in run.chunks)
        return REFLECT.format(state=run.final_state, notes=notes[-6000:])
    engine = MarkovianEngine(gen("handover", "fast"),
                             config or ChunkConfig(chunkSize=2000, maxChunks=24, carryoverTokens=256),
                             compressor=gen("handover", "fast"), reflector=gen("handover_reflect", "deep"),
                             **({"log_path": log_path} if log_path is not None else {}))
    run = engine.run(GOAL, inputs, mode="HANDOVER", system=SYSTEM, on_phase=on_phase, agent_id="handover",
                     reflect_prompt=reflect_prompt, early_final=False)  # every slice of the journal must be read
    out = {"events": len(events), "chunks": len(run.chunks), "completed": run.completed, "error": run.error,
           "note": run.reflection or run.final_state, "final_state": run.final_state, "run_id": run.id,
           "tokens": measure(run, inputs), "report": engine.report()}
    if shift_log_dir is not None and out["note"]:  # the human-readable shift log, one file per day
        out["shift_log"] = str(write_shift_log(out, shift_log_dir))
    return out


def rule_based_note(events: list[dict]) -> str:
    """The handover note computed from the journal by code: what SAMPLE_MODE writes instead of Nemotron's note."""
    finished = [e["data"] for e in events if e["kind"] == "finished"]
    kinds = Counter(d.get("kind", "?") for d in finished)
    judged = {e["data"].get("incident") for e in events if e["kind"] == "approval"}
    waiting = [d.get("incident") for d in finished if d.get("gate") == "awaiting_approval" and d.get("incident") not in judged]
    auto = sum(1 for d in finished if d.get("gate") == "auto_executed")
    rejected = sum(1 for e in events if e["kind"] == "approval" and not e["data"].get("approved"))
    count = lambda kind: sum(1 for e in events if e["kind"] == kind)  # noqa: E731
    usage = Counter(e["data"].get("resource", "?").split(":")[-1] for e in events if e["kind"] == "lease")
    return (f"Rule-based handover (sample mode, no model call). 1) {len(finished)} incidents finished "
            f"({', '.join(f'{k} {n}' for k, n in kinds.most_common()) or 'none'}); {auto} ran on their own. "
            f"2) Waiting for a person: {', '.join(waiting) if waiting else 'none'}. "
            f"3) Rejected by a person: {rejected}; approvals timed out: {count('approval_timeout')}; "
            f"pre-emptions: {count('preempt')}; recovered after a restart: {count('recovered')}. "
            f"4) Busiest: {', '.join(f'{r} {n}' for r, n in usage.most_common(3)) or 'no leases'}.")


def rule_based_chat(events: list[dict]) -> Callable:
    """SAMPLE_MODE stand-in for chat(): no model. Each chunk note counts the journal lines of its slice, and the
    final note is rule_based_note(); the Markovian engine (slices, carryover, token accounting) still runs."""
    seen: Counter = Counter()

    def chat(lane, messages, role=None, **_):
        prompt = messages[-1]["content"]
        if role == "handover_reflect":
            text = rule_based_note(events)
        else:
            lines = Counter(m.group(1) for m in re.finditer(r"^#\S+ (\w+)", prompt, re.M))
            seen.update(lines)
            text = (f"Slice: {', '.join(f'{k} {n}' for k, n in lines.most_common()) or 'no journal lines'}. "
                    f"{STATE} So far: {', '.join(f'{k} {n}' for k, n in seen.most_common())}")
        return SimpleNamespace(text=text, model="rule-based (sample mode)")
    return chat


def write_shift_log(out: dict, folder: Path) -> Path:
    from datetime import datetime
    now = datetime.now().astimezone()
    path = folder / f"shift-{now:%Y-%m-%d}.md"
    folder.mkdir(parents=True, exist_ok=True)
    head = "" if path.exists() else f"# Shift log {now:%Y-%m-%d}\n\nWritten by `run.py handover` (Meterless Markovian engine).\n"
    t = out["tokens"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{head}\n## Handover at {now:%H:%M %Z}\n\n{out['note'].strip()}\n\n"
                 f"_{out['events']} journal events in {out['chunks']} chunks; {t['markovian_prompt_tokens']} prompt tokens "
                 f"vs {t['standard_prompt_tokens']} re-sending history; run {out['run_id']}"
                 f"{'; incomplete: ' + out['error'] if out['error'] else ''}._\n")
    return path
