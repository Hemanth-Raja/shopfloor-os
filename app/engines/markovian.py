"""Meterless Markovian engine (headless), ported from the Meterless "Markovian Agent Engine" reference (v1.0).

A long reasoning task runs as a chain of bounded chunks. Each step sends only a short goal summary, the compressed
carryover from the previous step and that step's own input, so the context per step stays flat instead of growing
with the chain (O(1) per step instead of O(n) per step, O(n^2) in total). Pieces, following the spec's sections:

- ChunkConfig with the spec's defaults and bounds, clamped; the projected Standard-vs-Markovian curve as data (5.1)
- marker protocol: [STATE_CHECKPOINT] and [TASK_COMPLETE] (plus the back-compat markers), stripped by the engine (4)
- chunk manager with the `ceil(chars/4)` token estimate and the maxChunks cap (5.2)
- carryover compression cascade, in strict order: explicit state, injected compressor, heuristic key phrases,
  tail truncation (5.3)
- data-driven mode templates; a mode registers a first-chunk and a continuation template (5.4)
- the orchestrator loop with an injected generator, on_chunk/on_phase callbacks, abort, reflection (5.5, 8, 11)
- token and efficiency math (7), run records, cumulative stats and the projected/actual report (3, 9); every run
  is appended to logs/markovian.jsonl (5.7)

Token counts are the spec's heuristic (characters / 4), not a tokenizer; they are good for relative savings only.
Memory context goes into the first chunk only (6). No model calls happen here: the caller injects the generator.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

STATE, FINAL = "[STATE_CHECKPOINT]", "[TASK_COMPLETE]"
STATE_MARKERS = (STATE, "@@@STATE@@@", "[STATE]", "---STATE---")
FINAL_MARKERS = (FINAL, "@@@FINAL@@@", "[FINAL]", "---FINAL---")
BOUNDS = {"chunkSize": (1000, 128000, 1000), "carryoverTokens": (128, 32768, 128), "maxChunks": (1, 64, 1)}
LOG = Path(__file__).resolve().parents[2] / "logs" / "markovian.jsonl"
PHASES = ("initializing", "generating", "compressing", "reflecting", "complete", "error")
KEY_PHRASES = re.compile(r"(?:Therefore|Key insight|Status|Open|Risk|Next)[:,]?\s*([^.\n]{8,160})", re.I)


def tokens(text: str) -> int:
    return math.ceil(len(text or "") / 4)


@dataclass
class ChunkConfig:
    chunkSize: int = 8000
    maxChunks: int = 12
    carryoverTokens: int = 256
    overlapTokens: int = 128  # reserved by the spec

    def clamped(self) -> "ChunkConfig":
        out = ChunkConfig(**asdict(self))
        for k, (lo, hi, step) in BOUNDS.items():
            v = min(hi, max(lo, int(getattr(out, k))))
            setattr(out, k, lo + round((v - lo) / step) * step)
        return out

    def projected(self, steps: int = 10) -> list[dict]:
        """5.1: theoretical cost per step, as data."""
        return [{"step": s, "standard": (s - 1) * self.chunkSize + self.chunkSize,
                 "markovian": self.carryoverTokens + self.chunkSize} for s in range(1, steps + 1)]


@dataclass
class ChunkInfo:
    id: int
    tokens: int
    carryover: str  # the state that went into this step
    content: str  # clean output, markers stripped
    prompt_tokens: int = 0


@dataclass
class MarkovianRun:
    id: str
    timestamp: float
    prompt: str
    mode: str
    runId: str | None = None
    agentId: str | None = None
    chunks: list[ChunkInfo] = field(default_factory=list)
    totalTokensUsed: int = 0
    totalTokensSaved: int = 0
    efficiencyPercent: float = 0.0
    standardTokens: int = 0
    markovianTokens: int = 0
    durationMs: float = 0.0
    completed: bool = False
    aborted: bool = False
    error: str | None = None
    final_state: str = ""
    reflection: str | None = None

    @property
    def content(self) -> str:
        return "\n\n".join(c.content for c in self.chunks)


def split_markers(text: str) -> tuple[str, str | None, bool]:
    """-> (clean content, explicit state after a state marker or None, final marker seen)."""
    final = any(m in text for m in FINAL_MARKERS)
    state = None
    for m in STATE_MARKERS:
        if m in text:
            body, _, state = text.partition(m)
            text = body
            break
    for m in FINAL_MARKERS:
        text = text.replace(m, "")
        if state is not None:
            state = state.replace(m, "")
    return text.strip(), (state.strip() if state is not None else None), final


def _meaningful(s: str | None) -> bool:
    return bool(s) and len(re.sub(r"\W", "", s)) >= 12


def _truncate(text: str, budget: int) -> str:
    """4: keep the tail of the text within the carryover budget (budget * 4 characters, whole words)."""
    words, out, n = (text or "").split(), [], 0
    for w in reversed(words):
        if n + len(w) + 1 > budget * 4:
            break
        out.append(w)
        n += len(w) + 1
    return " ".join(reversed(out))


def compress(previous: str, content: str, explicit: str | None, budget: int,
             compressor: Callable[[str, str], str] | None = None) -> tuple[str, str]:
    """5.3 cascade, strict order. -> (carryover, tier used)."""
    if _meaningful(explicit):
        return _truncate(re.sub(r"\s+", " ", explicit), budget), "explicit"
    if compressor is not None:
        try:
            out = compressor(f"Previous state: {previous[:budget * 4]}\n\nNew output (preview): {content[:2000]}",
                             "Compress into 3-5 critical points, one line each, under "
                             f"{budget} tokens. Keep counts, ids and open risks.")
            if _meaningful(out):
                return _truncate(out.strip(), budget), "llm"
        except Exception:  # noqa: BLE001 - compression must survive a model or API failure
            pass
    found = [m.strip() for m in KEY_PHRASES.findall(content)]
    if found:
        return _truncate("; ".join(found[:5]), budget), "heuristic"
    return _truncate(f"{previous} {content}", budget), "truncate"


MODES: dict[str, dict[str, str]] = {
    "ARCHITECT": {
        "first": "Analyze the task, plan, and start.\n\n{memory}Task: {goal}\n\nInput:\n{input}\n\n{rules}",
        "next": "Goal (summary): {goal_short}\n\nState so far:\n{carryover}\n\nContinue from the state; do not repeat "
                "earlier work.\n\nInput:\n{input}\n\n{rules}",
    },
    "RESEARCH": {
        "first": "Investigate recursively. Log actions as '>> ...' and keep a research-tree snapshot.\n\n{memory}Task: "
                 "{goal}\n\nInput:\n{input}\n\n{rules}",
        "next": "Goal (summary): {goal_short}\n\nResearch state:\n{carryover}\n\nContinue the investigation.\n\nInput:\n"
                "{input}\n\n{rules}",
    },
}
RULES = (f"End with {STATE} followed by a concise state summary (counts, ids, open items). If this is the last input "
         f"and the task is done, end with {FINAL} instead.")


def register_mode(name: str, first: str, nxt: str) -> None:
    MODES[name] = {"first": first, "next": nxt}


def build_prompt(mode: str, goal: str, carryover: str, step_input: str, first: bool, memory: str = "",
                 last: bool = False) -> str:
    t = MODES[mode]["first" if first else "next"]
    rules = RULES + (f"\nThis is the LAST input: finish and end with {FINAL}." if last else "")
    return t.format(goal=goal, goal_short=goal[:200], carryover=carryover or "(none)", input=step_input,
                    memory=f"{memory}\n\n" if memory else "", rules=rules)


def account(run: MarkovianRun, carryover_tokens: int) -> None:
    """7: per-run totals. Standard re-sends all previous output each step; Markovian sends only the carryover."""
    toks = [c.tokens for c in run.chunks]
    run.totalTokensUsed = sum(toks)
    run.standardTokens = sum(sum(toks[:i]) + toks[i] for i in range(len(toks)))
    run.markovianTokens = len(toks) * carryover_tokens + run.totalTokensUsed
    run.totalTokensSaved = max(0, run.standardTokens - run.markovianTokens)
    run.efficiencyPercent = round(100 * run.totalTokensSaved / run.standardTokens, 1) if run.standardTokens else 0.0


class MarkovianEngine:
    """Headless engine: generator(prompt, system) -> text is injected; callbacks replace the UI."""

    def __init__(self, generator: Callable[[str, str], str], config: ChunkConfig | None = None,
                 compressor: Callable[[str, str], str] | None = None, reflector: Callable[[str, str], str] | None = None,
                 log_path: Path | None = LOG):
        self.generator, self.compressor, self.reflector = generator, compressor, reflector
        self.config = (config or ChunkConfig()).clamped()
        self.log_path = log_path
        self.history: list[MarkovianRun] = []

    def set_config(self, **kw) -> ChunkConfig:
        self.config = ChunkConfig(**{**asdict(self.config), **kw}).clamped()
        return self.config

    def run(self, goal: str, inputs: list[str], mode: str = "ARCHITECT", memory: str = "", system: str = "",
            on_chunk: Callable[[ChunkInfo], None] | None = None, on_phase: Callable[[str, str], None] | None = None,
            abort: Callable[[], bool] | None = None, run_id: str | None = None, agent_id: str | None = None,
            reflect_prompt: Callable[[MarkovianRun], str] | None = None) -> MarkovianRun:
        """Chain over `inputs` (one bounded input per chunk). Stops at the final marker, at maxChunks, or on abort."""
        cfg, started = self.config, time.perf_counter()
        run = MarkovianRun(id=uuid.uuid4().hex[:12], timestamp=time.time(), prompt=goal[:200], mode=mode,
                           runId=run_id, agentId=agent_id)
        phase = on_phase or (lambda p, d: None)
        phase("initializing", f"{len(inputs)} inputs, max {cfg.maxChunks} chunks")
        carry, tiers = "", []
        steps = inputs[:cfg.maxChunks]
        try:
            for i, step_input in enumerate(steps):
                if abort and abort():
                    run.aborted = True
                    break
                phase("generating", f"chunk {i + 1}/{len(steps)}")
                prompt = build_prompt(mode, goal, carry, step_input, first=i == 0, memory=memory if i == 0 else "",
                                      last=i == len(steps) - 1)
                raw = self.generator(prompt, system)
                content, explicit, final = split_markers(raw or "")
                chunk = ChunkInfo(id=i + 1, tokens=tokens(content), carryover=carry, content=content,
                                  prompt_tokens=tokens(prompt))
                if abort and abort():
                    run.chunks.append(chunk)
                    run.aborted = True
                    break
                phase("compressing", f"chunk {i + 1}")
                carry, tier = compress(carry, content, explicit, cfg.carryoverTokens, self.compressor)
                tiers.append(tier)
                run.chunks.append(chunk)
                if on_chunk:
                    on_chunk(chunk)
                if final:
                    run.completed = True
                    break
            else:
                run.completed = bool(run.chunks)  # every input consumed: the chain is done even without the marker
        except Exception as e:  # noqa: BLE001 - keep partial output (11)
            run.error = f"{type(e).__name__}: {str(e)[:200]}"
            phase("error", run.error)
        run.final_state = carry
        if run.completed and not run.aborted and run.chunks and self.reflector is not None:
            phase("reflecting", "synthesis")
            try:
                prompt = reflect_prompt(run) if reflect_prompt else self._reflect_prompt(goal, run)
                run.reflection = (self.reflector(prompt, system) or "").strip()
            except Exception as e:  # noqa: BLE001 - reflection failure must not fail the run (8)
                run.reflection = None
                run.error = run.error or f"reflection failed: {type(e).__name__}"
        account(run, cfg.carryoverTokens)
        run.durationMs = round((time.perf_counter() - started) * 1000, 1)
        self.history.append(run)
        self._persist(run, tiers)
        phase("complete" if run.error is None else "error", f"{len(run.chunks)} chunks, {run.efficiencyPercent}% saved")
        return run

    @staticmethod
    def _reflect_prompt(goal: str, run: MarkovianRun) -> str:
        body = run.content
        if len(body) > 12000:
            body = body[:6000] + "\n...\n" + body[-6000:]
        return f"Goal: {goal}\n\nFinal state: {run.final_state}\n\nWork so far:\n{body}\n\nWrite the final synthesis."

    def _persist(self, run: MarkovianRun, tiers: list[str]) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {k: v for k, v in asdict(run).items() if k != "chunks"}
        rec["chunks"] = [{"id": c.id, "tokens": c.tokens, "prompt_tokens": c.prompt_tokens,
                          "carryover_tokens": tokens(c.carryover)} for c in run.chunks]
        rec["compression_tiers"] = tiers
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    # 3.4 and 9: report data, never widgets
    def stats(self) -> dict:
        runs = self.history
        return {"totalRuns": len(runs), "completedRuns": sum(r.completed for r in runs),
                "totalTokensUsed": sum(r.totalTokensUsed for r in runs),
                "totalTokensSaved": sum(r.totalTokensSaved for r in runs),
                "averageEfficiency": round(sum(r.efficiencyPercent for r in runs) / len(runs), 1) if runs else 0.0,
                "totalChunksProcessed": sum(len(r.chunks) for r in runs),
                "lastRunTimestamp": runs[-1].timestamp if runs else None,
                "efficiencyHistory": [r.efficiencyPercent for r in runs]}

    def report(self) -> dict:
        actual = {}
        for r in self.history:
            history = 0
            for c in r.chunks:
                a = actual.setdefault(c.id, {"step": c.id, "n": 0, "standard": 0, "markovian": 0})
                a["n"] += 1
                a["standard"] += history + c.tokens
                a["markovian"] += self.config.carryoverTokens + c.tokens
                history += c.tokens
        rows = [{"step": a["step"], "n": a["n"], "standard": round(a["standard"] / a["n"]),
                 "markovian": round(a["markovian"] / a["n"])} for a in sorted(actual.values(), key=lambda a: a["step"])]
        return {"mode": "actual" if rows else "projected", "projected": self.config.projected(), "actual": rows,
                "config": asdict(self.config)}

    def get_run(self, run_id: str) -> MarkovianRun | None:
        return next((r for r in self.history if r.id == run_id), None)

    def list_runs(self) -> list[dict]:
        return [{"id": r.id, "mode": r.mode, "chunks": len(r.chunks), "efficiencyPercent": r.efficiencyPercent,
                 "completed": r.completed} for r in self.history]

    def delete_run(self, run_id: str) -> None:
        self.history = [r for r in self.history if r.id != run_id]

    def clear_history(self) -> None:
        self.history.clear()
