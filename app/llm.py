"""The only model-call layer: Meterless Nightshift via the OpenAI SDK.

    chat("deep"|"fast", messages) -> text, answering model, latency
    chat_json(...)                -> validated Pydantic model (one retry on schema miss)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError

load_dotenv()

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "llm.jsonl"
_LANES = {"deep": "NEMOTRON_DEEP", "fast": "NEMOTRON_FAST"}
_HEADERS = {"meterless-app": "shopfloor-os", "meterless-scope": "user"}


class LLMUnavailable(Exception):
    """Nightshift timed out or could not be reached; callers should switch to safe mode."""


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str
    latency_s: float
    truncated: bool = False


@dataclass(frozen=True)
class LLMJsonReply:
    data: BaseModel
    text: str
    model: str
    latency_s: float


def get_client(timeout_s: float = 20) -> OpenAI:
    return OpenAI(
        base_url=os.environ["MODEL_BASE_URL"],
        api_key=os.environ["MODEL_API_KEY"],
        default_headers=_HEADERS,
        timeout=timeout_s,
    )


def list_models() -> list[str]:
    return [m.id for m in get_client().models.list()]


def chat(lane: str, messages: list[dict[str, str]], max_tokens: int = 2000, timeout_s: float = 20) -> LLMReply:
    reply = _complete(lane, messages, max_tokens=max_tokens, timeout_s=timeout_s)
    if reply.truncated:
        reply = _complete(lane, messages, max_tokens=max_tokens * 2, timeout_s=timeout_s)
    return reply


def chat_json(
    lane: str,
    messages: list[dict[str, str]],
    schema: type[BaseModel],
    max_tokens: int = 2000,
    timeout_s: float = 20,
) -> LLMJsonReply:
    json_format = {"type": "json_object"}
    prompted = list(messages) + [
        {
            "role": "system",
            "content": (
                "Reply with a JSON object only, no markdown, matching this schema: "
                + json.dumps(schema.model_json_schema())
            ),
        }
    ]
    reply = _complete(
        lane, prompted, max_tokens=max_tokens, timeout_s=timeout_s,
        response_format=json_format,
    )
    err: Exception | None = None
    if reply.truncated:
        err = ValueError("reply truncated (finish_reason=length)")
    else:
        try:
            data = _parse(schema, reply.text)
        except (ValidationError, json.JSONDecodeError, ValueError) as parse_err:
            err = parse_err
        else:
            return LLMJsonReply(data=data, text=reply.text, model=reply.model, latency_s=reply.latency_s)
    retry_tokens = max_tokens * 2 if reply.truncated else max_tokens
    retry = list(prompted) + [
        {"role": "assistant", "content": reply.text},
        {
            "role": "user",
            "content": f"Your previous reply failed validation: {err}. Return JSON only that matches the schema.",
        },
    ]
    reply = _complete(
        lane, retry, max_tokens=retry_tokens, timeout_s=timeout_s,
        response_format=json_format,
    )
    if reply.truncated:
        raise ValueError("reply truncated (finish_reason=length)")
    data = _parse(schema, reply.text)
    return LLMJsonReply(data=data, text=reply.text, model=reply.model, latency_s=reply.latency_s)


def _model_for(lane: str) -> str:
    try:
        env_name = _LANES[lane]
    except KeyError as err:
        raise ValueError(f"lane must be 'deep' or 'fast', not {lane!r}") from err
    model = os.environ.get(env_name)
    if not model:
        raise RuntimeError(f"{env_name} is not set")
    return model


def _complete(
    lane: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout_s: float,
    response_format: dict[str, str] | None = None,
) -> LLMReply:
    requested = _model_for(lane)
    kwargs: dict[str, Any] = {
        "model": requested,
        "messages": messages,
        "max_tokens": max_tokens,
        "timeout": timeout_s,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    started = time.perf_counter()
    try:
        reply = get_client(timeout_s).chat.completions.create(**kwargs)
    except (APITimeoutError, APIConnectionError) as err:
        raise LLMUnavailable(str(err)) from err
    latency = time.perf_counter() - started
    text = ""
    finish_reason = None
    if reply.choices:
        choice = reply.choices[0]
        text = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)
    truncated = finish_reason == "length"
    answering = reply.model or requested
    _log_call(lane, requested, answering, latency, getattr(reply, "usage", None), truncated)
    return LLMReply(text=text, model=answering, latency_s=latency, truncated=truncated)


def _parse(schema: type[BaseModel], text: str) -> BaseModel:
    return schema.model_validate_json(_json_payload(text))


def _json_payload(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _tokens(usage: Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        completion = getattr(usage, "output_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if prompt is None and completion is None and total is None:
        return None
    return {"prompt": prompt, "completion": completion, "total": total}


def _log_call(lane: str, requested: str, answering: str, latency_s: float, usage: Any, truncated: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lane": lane,
        "requested_model": requested,
        "answering_model": answering,
        "latency_s": round(latency_s, 4),
        "tokens": _tokens(usage),
        "truncated": truncated,
    }
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
