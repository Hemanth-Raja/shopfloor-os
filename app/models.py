"""Model registry: which model serves which agent role, with fallback chains and per-model circuit breakers.

    chat_json(lane, messages, schema, role="sql_agent")  -> reply with .data, .model (answering), .requested, ...
    chat(lane, messages, role="monitor")                  -> reply with .text, .model, ...

The role -> chain mapping lives in config/models.json. Each call tries the chain in order. A model whose breaker
is open is skipped; a timeout or connection error opens it after `breaker_threshold` failures and the next model
in the chain answers. All calls go through Meterless Nightshift with the same client and headers as app/llm.py
(this module only imports from it). `lane` stays in the signature so recorded SAMPLE_MODE replies keep their keys;
without a role, the lane picks one (deep -> planner, fast -> monitor).

Every call records the requested and the answering model (Nightshift may serve another model than the one
requested), the latency, the token budget and the id of Nightshift's signed receipt for that request.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError

from app import llm
from app.agents.health import CircuitBreaker, CircuitOpen
from app.llm import LLMUnavailable
from app.resilience import AIMDLimiter, Deadline, DeadlineExceeded, LatencyStats, LimiterTimeout, backoff_delay, repair_json

CONFIG = Path(__file__).resolve().parents[1] / "config" / "models.json"
_lock = threading.Lock()
LAST: dict[str, dict] = {}  # role -> last call (for the Models panel)


def load_config(path: Path = CONFIG) -> dict:
    cfg = json.loads(path.read_text())
    for role, spec in cfg["roles"].items():
        if not spec.get("chain") or not all(isinstance(m, str) and "/" in m for m in spec["chain"]):
            raise ValueError(f"config/models.json: role {role} needs a chain of model ids")
    return cfg


CFG = load_config()
BREAKERS: dict[str, CircuitBreaker] = {}


def breaker(model: str) -> CircuitBreaker:
    with _lock:
        if model not in BREAKERS:
            d = CFG["defaults"]
            BREAKERS[model] = CircuitBreaker(f"model {model}", threshold=d["breaker_threshold"],
                                             cooldown_s=d["breaker_cooldown_s"])
        return BREAKERS[model]


def role_for(lane: str, role: str | None) -> str:
    role = role or CFG["defaults"]["lane_roles"].get(lane, "planner")
    if role not in CFG["roles"]:
        raise ValueError(f"unknown model role {role!r}")
    return role


def receipt_for(request_id: str | None, wait_s: float = 1.0) -> str | None:
    """Nightshift's signed receipt id for one request (matched on the OpenAI response id), if it is readable."""
    from app.powered import receipts_path
    path = receipts_path()
    if not request_id or path is None or not path.exists():
        return None
    deadline = time.monotonic() + wait_s
    while True:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 262144))
            for line in reversed(fh.read().splitlines()):
                if request_id.encode() in line:
                    try:
                        body = json.loads(line)["body"]
                        return (json.loads(body) if isinstance(body, str) else body)["receipt"]["receipt_id"]
                    except (ValueError, KeyError, TypeError):
                        return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _complete(model: str, role: str, messages: list[dict], max_tokens: int, timeout_s: float,
              json_mode: bool) -> SimpleNamespace:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "timeout": timeout_s}
    # the app pinned to this model in Nightshift (a fallback model goes out under its own app), else the role's app
    app = CFG.get("model_apps", {}).get(model) or CFG["roles"].get(role, {}).get("app")
    if app:  # Nightshift routes by the app's rule (mode, lane allowlist), not by the model id we send
        kwargs["extra_headers"] = {"meterless-app": app}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    try:
        # no hidden SDK retries: our own retry loop below decides (backoff, jitter, deadline)
        reply = llm.get_client(timeout_s).with_options(max_retries=0).chat.completions.create(**kwargs)
    except (APITimeoutError, APIConnectionError) as err:
        raise LLMUnavailable(str(err)) from err
    except APIStatusError as err:
        if err.status_code >= 500 or err.status_code == 429:
            raise LLMUnavailable(f"HTTP {err.status_code}") from err
        raise
    latency = time.perf_counter() - started
    choice = reply.choices[0] if reply.choices else None
    text = (choice.message.content or "") if choice else ""
    truncated = getattr(choice, "finish_reason", None) == "length"
    answering = reply.model or model
    llm._log_call(role, model, answering, latency, getattr(reply, "usage", None), truncated)
    if not text.strip() and not truncated:
        raise LLMUnavailable("empty reply")  # transient: worth a retry
    return SimpleNamespace(text=text, model=answering, requested=model, latency_s=latency, truncated=truncated,
                           request_id=getattr(reply, "id", None), tokens=llm._tokens(getattr(reply, "usage", None)))


class ContextTooLarge(ValueError):
    """The prompt would not fit the model's context budget; the next model in the chain is tried."""


def estimate_tokens(messages: list[dict]) -> int:
    """A conservative estimate (about 3.5 characters per token for English and JSON)."""
    return int(sum(len(str(m.get("content", ""))) for m in messages) / 3.5) + 8 * len(messages)


def check_context(model: str, messages: list[dict], max_tokens: int) -> int:
    """Refuse a prompt that would use more than `context_fraction` of the model's context window."""
    window = CFG.get("context_tokens", {}).get(model, 131072)
    budget = int(window * CFG["defaults"].get("context_fraction", 0.5))
    need = estimate_tokens(messages) + max_tokens
    if need > budget:
        raise ContextTooLarge(f"about {need} tokens needed, budget {budget} of {window} for {model}")
    return need


# ---------------------------------------------------------------- resilience: retries, limiter, hedging, deadline

LIMITERS: dict[str, AIMDLimiter] = {}
LATENCY = LatencyStats()
RETRIES = {"n": 0}  # counters for Health
SLEEP = time.sleep  # tests replace it


def limiter(model: str) -> AIMDLimiter:
    with _lock:
        if model not in LIMITERS:
            d = CFG["defaults"]
            cap = os.environ.get("MODEL_CONCURRENCY_MAX")  # live demos and eval runs pin it (e.g. 2) for the free endpoints
            high = int(cap) if cap else d.get("concurrency_max", 8)
            LIMITERS[model] = AIMDLimiter(start=min(d.get("concurrency_start", 3), high), high=high,
                                          slow_s=d.get("slow_reply_s", 60))
        return LIMITERS[model]


def _attempt(model: str, spec: dict, call, deadline: Deadline) -> SimpleNamespace:
    """One model: limiter slot, then up to `retries` retries with full-jitter backoff on transient errors.
    The breaker counts the whole attempt once, so retries do not trip it on their own."""
    retries = CFG["defaults"].get("retries", 2)

    def run():
        last = None
        for n in range(retries + 1):
            timeout = deadline.cap(spec["budget_s"])
            lim = limiter(model)
            try:
                with lim.slot(min(timeout, 5.0)):
                    started = time.perf_counter()
                    out = call(model, spec, timeout)
                lim.success(time.perf_counter() - started)
                LATENCY.add(spec["_role"], out.latency_s)
                out.retries = n
                return out
            except (LLMUnavailable, LimiterTimeout) as err:
                if isinstance(err, CircuitOpen):
                    raise
                lim.failure()
                last = err
                if n < retries:
                    RETRIES["n"] += 1
                    pause = backoff_delay(n)
                    if deadline.left() - pause > 1.0:
                        SLEEP(pause)
                        continue
                raise
        raise last  # pragma: no cover
    return breaker(model).call(run)


def _race(first: str, second: str, spec: dict, call, deadline: Deadline, tried: list) -> SimpleNamespace | None:
    """Hedged request: if `first` is slower than the role's median, also ask `second`; the first valid reply wins."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    hedge_after = LATENCY.p50(spec["_role"], default=spec["budget_s"] / 4) * CFG["defaults"].get("hedge_factor", 1.5)
    pool = ThreadPoolExecutor(max_workers=2)
    futures = {pool.submit(_attempt, first, spec, call, deadline): first}
    done, _ = wait(futures, timeout=min(hedge_after, max(0.0, deadline.left())))
    if not done and breaker(second).state != "OPEN":
        futures[pool.submit(_attempt, second, spec, call, deadline)] = second
        tried.append({"model": second, "hedged_after_s": round(hedge_after, 1)})
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=max(0.0, deadline.left()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                try:
                    return f.result()
                except Exception as err:  # noqa: BLE001
                    tried.append({"model": futures[f], "error": f"{type(err).__name__}: {str(err)[:120]}"})
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _over_chain(lane: str, role: str | None, call, messages: list[dict] | None = None,
                deadline: Deadline | None = None) -> SimpleNamespace:
    role = role_for(lane, role)
    spec = {**CFG["roles"][role], "_role": role}
    deadline = deadline or Deadline(seconds=spec["budget_s"] * len(spec["chain"]) + 5)
    tried: list = []
    models = []
    for model in effective_chain(role):
        if messages is not None:
            try:
                check_context(model, messages, spec["max_tokens"])
            except ContextTooLarge as err:
                tried.append({"model": model, "skipped": f"context: {err}"})
                continue
        models.append(model)
    i = 0
    while i < len(models):
        if deadline.left() <= 0.5:
            tried.append({"skipped": "deadline: the incident's time budget is spent"})
            break
        model = models[i]
        out = None
        if spec.get("hedge") and i + 1 < len(models) and breaker(model).state != "OPEN":
            out = _race(model, models[i + 1], spec, call, deadline, tried)
            i += 2
        else:
            i += 1
            try:
                out = _attempt(model, spec, call, deadline)
            except CircuitOpen:
                tried.append({"model": model, "skipped": "breaker open"})
            except DeadlineExceeded:
                tried.append({"model": model, "skipped": "deadline"})
                break
            except Exception as err:  # noqa: BLE001 - timeout, HTTP error or an unusable reply: next model in the chain
                tried.append({"model": model, "error": f"{type(err).__name__}: {str(err)[:120]}"})
        if out is None:
            continue
        out.role, out.tried = role, tried
        out.fell_back = any("error" in t or "skipped" in t for t in tried) or out.model != out.requested
        out.receipt = receipt_for(out.request_id)
        note_answer(out.requested, out.model)
        LAST[role] = {"role": role, "requested": out.requested, "answering": out.model,
                      "latency_s": round(out.latency_s, 2), "receipt": out.receipt, "at": time.time(),
                      "fell_back": out.fell_back, "retries": getattr(out, "retries", 0)}
        return out
    raise LLMUnavailable(f"every model for role {role} failed or is open: {tried}")


def _deadline(timeout_s, deadline) -> Deadline | None:
    if isinstance(deadline, Deadline):
        return deadline
    if isinstance(deadline, (int, float)):
        return Deadline(at=float(deadline))
    return None


def chat(lane: str, messages: list[dict], max_tokens: int | None = None, timeout_s: float | None = None,
         role: str | None = None, deadline=None) -> SimpleNamespace:
    spec_role = role_for(lane, role)

    def call(model, spec, timeout):
        return _complete(model, spec_role, messages, max_tokens or spec["max_tokens"],
                         min(timeout_s or timeout, timeout), json_mode=False)
    return _over_chain(lane, role, call, messages, _deadline(timeout_s, deadline))


def call_info(reply) -> dict:
    """Requested and answering model, latency and receipt of one reply (replayed replies have fewer fields)."""
    return {"requested": getattr(reply, "requested", None), "answering": getattr(reply, "model", None),
            "latency_s": round(getattr(reply, "latency_s", 0.0) or 0.0, 2), "receipt": getattr(reply, "receipt", None),
            "role": getattr(reply, "role", None), "fell_back": getattr(reply, "fell_back", None),
            "retries": getattr(reply, "retries", None), "repaired": getattr(reply, "repaired", None)}


def _parse_or_repair(schema: type[BaseModel], text: str):
    """Validate; on failure try a mechanical JSON repair before spending a re-ask. -> (data, repaired)"""
    try:
        return llm._parse(schema, text), False
    except (ValidationError, json.JSONDecodeError, ValueError):
        return schema.model_validate_json(repair_json(text)), True


def chat_json(lane: str, messages: list[dict], schema: type[BaseModel], max_tokens: int | None = None,
              timeout_s: float | None = None, role: str | None = None, deadline=None) -> SimpleNamespace:
    """Strict JSON: the schema is in the prompt, the reply is validated (repaired if needed); one re-ask on the same
    model on a miss; then the next model."""
    spec_role = role_for(lane, role)
    prompted = list(messages) + [{"role": "system", "content": "Reply with a JSON object only, no markdown, matching "
                                  "this schema: " + json.dumps(schema.model_json_schema())}]

    def call(model, spec, timeout):
        budget = min(timeout_s or timeout, timeout)
        tokens = max_tokens or spec["max_tokens"]
        reply = _complete(model, spec_role, prompted, tokens, budget, json_mode=True)
        try:
            if reply.truncated:
                raise ValueError("reply truncated (finish_reason=length)")
            reply.data, reply.repaired = _parse_or_repair(schema, reply.text)
            return reply
        except (ValidationError, json.JSONDecodeError, ValueError) as err:
            retry = prompted + [{"role": "assistant", "content": reply.text},
                                {"role": "user", "content": f"Your previous reply failed validation: {err}. "
                                                            "Return JSON only that matches the schema."}]
            again = _complete(model, spec_role, retry, tokens * (2 if reply.truncated else 1), budget, json_mode=True)
            again.data, again.repaired = _parse_or_repair(schema, again.text)
            return again
    return _over_chain(lane, role, call, prompted, _deadline(timeout_s, deadline))


# ---------------------------------------------------------------- self-check and pin drift

EFFECTIVE: dict[str, dict] = {}  # model -> {"answers_as", "ok", "latency_s", "checked_at", "error"}
DRIFT: dict[str, dict] = {}  # model -> {"calls", "mismatched"} from live traffic


def note_answer(requested: str, answering: str) -> None:
    with _lock:
        d = DRIFT.setdefault(requested, {"calls": 0, "mismatched": 0})
        d["calls"] += 1
        d["mismatched"] += int(answering != requested)


def self_check(timeout_s: float = 60) -> dict:
    """One tiny call per pinned model, under its own Nightshift app: who really answers? Builds the effective registry
    (a model served by another one is skipped in chains that already contain that other model)."""
    for model, app in CFG.get("model_apps", {}).items():
        t = time.perf_counter()
        try:
            r = _complete(model, "router" if "lightning" in model else "planner",
                          [{"role": "user", "content": "Reply with exactly one word: OK"}], 800, timeout_s, False)
            EFFECTIVE[model] = {"app": app, "answers_as": r.model, "ok": True, "latency_s": round(r.latency_s, 2),
                                "checked_at": time.time(), "drift": r.model != model}
        except Exception as err:  # noqa: BLE001
            EFFECTIVE[model] = {"app": app, "answers_as": None, "ok": False, "checked_at": time.time(),
                                "latency_s": round(time.perf_counter() - t, 2), "error": f"{type(err).__name__}: {str(err)[:100]}"}
    return dict(EFFECTIVE)


def effective_chain(role: str) -> list[str]:
    """The role's chain without models that failed the self-check or are silently served by another model in it."""
    chain = CFG["roles"][role]["chain"]
    out = []
    for m in chain:
        e = EFFECTIVE.get(m)
        if e is None:
            out.append(m)  # not checked: keep
        elif e["ok"] and (e["answers_as"] == m or e["answers_as"] not in chain):
            out.append(m)
    return out or list(chain)  # never empty: the breakers and fallbacks still protect us


def pin_report() -> dict:
    """For Health: per pinned model, the self-check and the live drift rate; warnings when a pin drifted."""
    out, warnings = {}, []
    for model, app in CFG.get("model_apps", {}).items():
        e, d = EFFECTIVE.get(model, {}), DRIFT.get(model, {"calls": 0, "mismatched": 0})
        rate = round(d["mismatched"] / d["calls"], 2) if d["calls"] else None
        out[model] = {"app": app, "self_check": e or None, "live_calls": d["calls"], "live_drift_rate": rate}
        if (e and (not e.get("ok") or e.get("drift"))) or (rate is not None and d["calls"] >= 3 and rate > 0.3):
            warnings.append(f"{app}: pinned to {model.split('/')[-1]} but answered by "
                            f"{(e.get('answers_as') or 'nothing').split('/')[-1]} (live drift {rate})")
    return {"pins": out, "warnings": warnings}


def mapping() -> list[dict]:
    """Role -> chain, why, breaker states and the last call, for the UI's Models panel and the health agent."""
    probe_path = Path(__file__).resolve().parents[1] / "data" / "model_probe.json"
    probe = {}
    if probe_path.exists():
        probe = {r["requested"]: r for r in json.loads(probe_path.read_text()).get("results", [])}
    out = []
    for role, spec in CFG["roles"].items():
        out.append({"role": role, "chain": spec["chain"], "why": spec["why"], "budget_s": spec["budget_s"],
                    "app": spec.get("app", "shopfloor-os"),
                    "max_tokens": spec["max_tokens"],
                    "breakers": {m: BREAKERS[m].state if m in BREAKERS else "CLOSED" for m in spec["chain"]},
                    "served_by": {m: (probe.get(m) or {}).get("answering") for m in spec["chain"]},
                    "last": LAST.get(role), "hedge": bool(spec.get("hedge")),
                    "checked": {m: (EFFECTIVE.get(m) or {}).get("answers_as") or (("failed" if m in EFFECTIVE else None))
                                for m in spec["chain"]},
                    "limiters": {m: LIMITERS[m].state() for m in spec["chain"] if m in LIMITERS},
                    "p50_s": round(LATENCY.p50(role, default=0.0), 1) or None})
    return out
