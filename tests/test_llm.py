"""JSON validation and logging for app.llm, using a fake Nightshift client (no network)."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app import llm


class Answer(BaseModel):
    value: int


class FakeClient:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("fake client has no more replies")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _reply(text, model="answered-model", usage=None, finish_reason="stop"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)],
        usage=usage,
    )


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NEMOTRON_DEEP", "deep-model")
    monkeypatch.setenv("NEMOTRON_FAST", "fast-model")
    monkeypatch.setenv("MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MODEL_API_KEY", "test-key-not-for-use")
    log_path = tmp_path / "llm.jsonl"
    monkeypatch.setattr(llm, "LOG_PATH", log_path)
    return log_path


def test_chat_json_validates(monkeypatch, fake_env):
    client = FakeClient([_reply('{"value": 7}')])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    result = llm.chat_json("fast", [{"role": "user", "content": "give a value"}], Answer)

    assert result.data.value == 7
    assert result.model == "answered-model"
    assert result.latency_s >= 0
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "fast-model"
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_chat_json_retries_once_on_validation_error(monkeypatch, fake_env):
    client = FakeClient([_reply('{"value": "nope"}'), _reply('{"value": 3}')])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    result = llm.chat_json("deep", [{"role": "user", "content": "SECRET_PROMPT_BODY"}], Answer)

    assert result.data.value == 3
    assert len(client.calls) == 2
    retry_messages = client.calls[1]["messages"]
    assert any("validation" in m["content"].lower() for m in retry_messages if m["role"] == "user")


def test_chat_json_raises_after_retry_still_invalid(monkeypatch, fake_env):
    client = FakeClient([_reply("{}"), _reply('{"value": "still-bad"}')])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    with pytest.raises(ValidationError):
        llm.chat_json("deep", [{"role": "user", "content": "n"}], Answer)


def test_log_has_metadata_not_content_or_secrets(monkeypatch, fake_env):
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15)
    client = FakeClient([_reply("plain text, SECRET_PROMPT_BODY", usage=usage)])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    llm.chat("deep", [{"role": "user", "content": "SECRET_PROMPT_BODY tell me a secret"}])

    raw = fake_env.read_text(encoding="utf-8").strip()
    assert raw
    line = raw.splitlines()[0]
    record = json.loads(line)
    assert record["lane"] == "deep"
    assert record["requested_model"] == "deep-model"
    assert record["answering_model"] == "answered-model"
    assert "latency_s" in record
    assert record["tokens"] == {"prompt": 11, "completion": 4, "total": 15}
    assert record["truncated"] is False
    assert "SECRET_PROMPT_BODY" not in line
    assert "test-key-not-for-use" not in line
    assert "plain text" not in line
    assert "content" not in record
    assert "messages" not in record
    assert "api_key" not in record


def test_truncation_retries_with_double_max_tokens(monkeypatch, fake_env):
    client = FakeClient([
        _reply("cut off mid-sentence", finish_reason="length"),
        _reply("full answer", finish_reason="stop"),
    ])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    result = llm.chat("deep", [{"role": "user", "content": "summarise"}])

    assert result.text == "full answer"
    assert result.truncated is False
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] == 2000
    assert client.calls[1]["max_tokens"] == 4000
    lines = [json.loads(line) for line in fake_env.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["truncated"] is True
    assert lines[1]["truncated"] is False


def test_chat_json_does_not_parse_truncated_json(monkeypatch, fake_env):
    client = FakeClient([
        _reply('{"value": 1}', finish_reason="length"),
        _reply('{"value": 2}', finish_reason="stop"),
    ])
    monkeypatch.setattr(llm, "get_client", lambda timeout_s=20: client)

    result = llm.chat_json("deep", [{"role": "user", "content": "give a value"}], Answer)

    assert result.data.value == 2
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] == 2000
    assert client.calls[1]["max_tokens"] == 4000
    retry_user = [m for m in client.calls[1]["messages"] if m["role"] == "user"][-1]
    assert "truncated" in retry_user["content"].lower()
