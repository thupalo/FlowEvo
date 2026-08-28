"""LLMClient tests with a monkeypatched ``requests.post`` — no network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime import llm_client as mod  # noqa: E402
from runtime.config import GenerationSettings, RuntimeLLMConfig, SkillContextBudgets  # noqa: E402
from runtime.llm_client import LLMClient, LLMClientError  # noqa: E402


def make_config(**overrides) -> RuntimeLLMConfig:
    base = dict(
        provider="openrouter",
        api_key="k",
        base_url="http://localhost:8000/v1",
        model="m",
        app_name="t",
        skill_top_k=3,
        skill_context_budgets=SkillContextBudgets(1, 1, 1, 1),
        draft=GenerationSettings(0.0, 100),
        repair=GenerationSettings(0.0, 100),
        config_path="",
        local_override_path="",
    )
    base.update(overrides)
    return RuntimeLLMConfig(**base)


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = text or (str(body) if body is not None else "")
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def chat(content: str, finish_reason: str = "stop", reasoning: str = "", usage_completion: int = 5) -> dict:
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning"] = reasoning
    return {
        "choices": [{"finish_reason": finish_reason, "message": msg}],
        "usage": {"prompt_tokens": 10, "completion_tokens": usage_completion, "total_tokens": 10 + usage_completion},
    }


@pytest.fixture
def post(monkeypatch):
    calls: list[dict] = []
    script: list = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "max_tokens": json["max_tokens"]})
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return script, calls


def test_response_carries_finish_reason_and_reasoning(post):
    script, _ = post
    script.append(FakeResponse(200, chat("SELECT 1", finish_reason="stop", reasoning="thinking...")))
    resp = LLMClient(make_config()).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 100))
    assert resp.text == "SELECT 1"
    assert resp.finish_reason == "stop"
    assert resp.reasoning == "thinking..."


def test_empty_content_raises_with_finish_reason_and_no_growth_by_default(post):
    script, calls = post
    script.append(FakeResponse(200, chat("", finish_reason="length", reasoning="x" * 100)))
    with pytest.raises(LLMClientError) as ei:
        LLMClient(make_config()).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 100))
    assert "empty content" in str(ei.value)
    assert ei.value.finish_reason == "length"
    assert "reasoning_chars=100" in str(ei.value)
    assert [c["max_tokens"] for c in calls] == [100]


def test_grow_on_truncation_doubles_budget_until_content(post):
    script, calls = post
    script.append(FakeResponse(200, chat("", finish_reason="length")))
    script.append(FakeResponse(200, chat("", finish_reason="length")))
    script.append(FakeResponse(200, chat("answer", finish_reason="stop")))
    cfg = make_config(grow_on_truncation=True, max_output_tokens_cap=16384)
    resp = LLMClient(cfg).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 600))
    assert resp.text == "answer"
    assert [c["max_tokens"] for c in calls] == [600, 1200, 2400]


def test_grow_on_truncation_stops_at_cap_and_reports(post):
    script, calls = post
    script.extend(FakeResponse(200, chat("", finish_reason="length")) for _ in range(10))
    cfg = make_config(grow_on_truncation=True, max_output_tokens_cap=2000)
    with pytest.raises(LLMClientError) as ei:
        LLMClient(cfg).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 600))
    assert [c["max_tokens"] for c in calls] == [600, 1200, 2000]
    assert "budget increases" in str(ei.value)


def test_grow_retries_truncated_non_empty_content(post):
    script, calls = post
    script.append(FakeResponse(200, chat("```sql\nSELECT c.first", finish_reason="length")))
    script.append(FakeResponse(200, chat("```sql\nSELECT 1\n```", finish_reason="stop")))
    cfg = make_config(grow_on_truncation=True)
    resp = LLMClient(cfg).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 150))
    assert resp.text.endswith("```") and resp.finish_reason == "stop"
    assert [c["max_tokens"] for c in calls] == [150, 300]


def test_truncated_content_is_returned_with_finish_reason_when_growth_off(post):
    script, calls = post
    script.append(FakeResponse(200, chat("partial", finish_reason="length")))
    resp = LLMClient(make_config()).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 150))
    assert resp.text == "partial" and resp.finish_reason == "length" and len(calls) == 1


def test_grow_does_not_apply_when_finish_reason_is_not_length(post):
    script, calls = post
    script.append(FakeResponse(200, chat("", finish_reason="stop")))
    cfg = make_config(grow_on_truncation=True)
    with pytest.raises(LLMClientError):
        LLMClient(cfg).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 600))
    assert len(calls) == 1


def test_http_error_carries_status_code(post):
    script, _ = post
    script.append(FakeResponse(401, text="unauthorized"))
    with pytest.raises(LLMClientError) as ei:
        LLMClient(make_config()).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 100))
    assert ei.value.status_code == 401
    assert "HTTP 401" in str(ei.value)


def test_transient_status_is_retried_then_succeeds(post):
    script, calls = post
    script.append(FakeResponse(503, text="down"))
    script.append(FakeResponse(200, chat("ok")))
    resp = LLMClient(make_config()).generate(instructions="", input_text="q", settings=GenerationSettings(0.0, 100))
    assert resp.text == "ok" and len(calls) == 2
