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
    app = CFG["roles"].get(role, {}).get("app")
    if app:  # Nightshift routes by the app's rule (mode, lane allowlist), not by the model id we send
        kwargs["extra_headers"] = {"meterless-app": app}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    try:
        # no hidden SDK retries: a failing model hands over to the next one in the chain at once
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
    return SimpleNamespace(text=text, model=answering, requested=model, latency_s=latency, truncated=truncated,
                           request_id=getattr(reply, "id", None), tokens=llm._tokens(getattr(reply, "usage", None)))


def _over_chain(lane: str, role: str | None, call) -> SimpleNamespace:
    role = role_for(lane, role)
    spec = CFG["roles"][role]
    tried = []
    for model in spec["chain"]:
        b = breaker(model)
        try:
            out = b.call(call, model, spec)
        except CircuitOpen:
            tried.append({"model": model, "skipped": "breaker open"})
            continue
        except Exception as err:  # noqa: BLE001 - timeout, HTTP error or an unusable reply: next model in the chain
            tried.append({"model": model, "error": f"{type(err).__name__}: {str(err)[:120]}"})
            continue
        out.role, out.tried = role, tried
        out.fell_back = bool(tried) or out.model != out.requested
        out.receipt = receipt_for(out.request_id)
        LAST[role] = {"role": role, "requested": out.requested, "answering": out.model,
                      "latency_s": round(out.latency_s, 2), "receipt": out.receipt, "at": time.time(),
                      "fell_back": out.fell_back}
        return out
    raise LLMUnavailable(f"every model for role {role} failed or is open: {tried}")


def chat(lane: str, messages: list[dict], max_tokens: int | None = None, timeout_s: float | None = None,
         role: str | None = None) -> SimpleNamespace:
    spec_role = role_for(lane, role)

    def call(model, spec):
        return _complete(model, spec_role, messages, max_tokens or spec["max_tokens"],
                         min(timeout_s or spec["budget_s"], spec["budget_s"]), json_mode=False)
    return _over_chain(lane, role, call)


def call_info(reply) -> dict:
    """Requested and answering model, latency and receipt of one reply (replayed replies have fewer fields)."""
    return {"requested": getattr(reply, "requested", None), "answering": getattr(reply, "model", None),
            "latency_s": round(getattr(reply, "latency_s", 0.0) or 0.0, 2), "receipt": getattr(reply, "receipt", None),
            "role": getattr(reply, "role", None), "fell_back": getattr(reply, "fell_back", None)}


def chat_json(lane: str, messages: list[dict], schema: type[BaseModel], max_tokens: int | None = None,
              timeout_s: float | None = None, role: str | None = None) -> SimpleNamespace:
    """Strict JSON: the schema is in the prompt, the reply is validated; one retry on the same model on a miss."""
    spec_role = role_for(lane, role)
    prompted = list(messages) + [{"role": "system", "content": "Reply with a JSON object only, no markdown, matching "
                                  "this schema: " + json.dumps(schema.model_json_schema())}]

    def call(model, spec):
        budget = min(timeout_s or spec["budget_s"], spec["budget_s"])
        tokens = max_tokens or spec["max_tokens"]
        reply = _complete(model, spec_role, prompted, tokens, budget, json_mode=True)
        try:
            if reply.truncated:
                raise ValueError("reply truncated (finish_reason=length)")
            reply.data = llm._parse(schema, reply.text)
            return reply
        except (ValidationError, json.JSONDecodeError, ValueError) as err:
            retry = prompted + [{"role": "assistant", "content": reply.text},
                                {"role": "user", "content": f"Your previous reply failed validation: {err}. "
                                                            "Return JSON only that matches the schema."}]
            again = _complete(model, spec_role, retry, tokens * (2 if reply.truncated else 1), budget, json_mode=True)
            again.data = llm._parse(schema, again.text)
            return again
    return _over_chain(lane, role, call)


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
                    "last": LAST.get(role)})
    return out
