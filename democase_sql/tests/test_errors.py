"""Tests for error classification and the generator's token-limit handling.

Uses a fake client so no network / API key is required."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from runtime.llm_client import LLMClientError  # noqa: E402

from democase_sql import errors  # noqa: E402
from democase_sql.errors import LLMGenerationError, RunAborted, classify_client_error  # noqa: E402
from democase_sql.generator import LLMSqlGenerator, looks_truncated  # noqa: E402
from democase_sql.schemas import SchemaExemplar, SchemaInsight, SqlTask  # noqa: E402


@dataclass
class FakeResp:
    text: str
    prompt_tokens: int = 100
    completion_tokens: int = 50
    latency_ms: float = 1.0


class FakeClient:
    """Scripted client: each entry is either a FakeResp, an exception, or a
    callable(settings) -> FakeResp | exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def generate(self, *, instructions, input_text, settings):
        self.calls.append({"max_tokens": settings.max_output_tokens, "prompt_len": len(input_text)})
        item = self.script.pop(0)
        if callable(item):
            item = item(settings)
        if isinstance(item, BaseException):
            raise item
        return item


TASK = SqlTask(task_id="t", question='Email of "Alice Smith"?', gold_sql="SELECT 1", pattern="x")
GOOD = FakeResp("```sql\nSELECT email FROM customers\n```")


def make(script, **kw) -> tuple[LLMSqlGenerator, FakeClient]:
    client = FakeClient(script)
    return LLMSqlGenerator(client=client, max_output_tokens=kw.pop("max_output_tokens", 1024), **kw), client


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg, kind, status",
    [
        ("OpenRouter returned empty content.", errors.EMPTY_OUTPUT, None),
        ("HTTP 401 for http://x/v1/chat/completions. Body: unauthorized", errors.AUTH, 401),
        ("HTTP 403 for u. Body: forbidden", errors.AUTH, 403),
        ("HTTP 429 for u. Body: slow down", errors.RATE_LIMIT, 429),
        ("HTTP 503 for u. Body: overloaded", errors.SERVER, 503),
        ("HTTP 400 for u. Body: This model's maximum context length is 8192 tokens", errors.CONTEXT_LENGTH, 400),
        ("HTTP 400 for u. Body: model not found", errors.BAD_REQUEST, 400),
        ("ConnectionError: refused", errors.TRANSPORT, None),
        ("Exceeded retry limit after 4 transient retries. Last error: ReadTimeout", errors.TRANSPORT, None),
        ("Invalid JSON: <html>", errors.MALFORMED, None),
        ("something weird", errors.UNKNOWN, None),
    ],
)
def test_classify(msg, kind, status):
    assert classify_client_error(LLMClientError(msg)) == (kind, status)


def test_fatal_and_retryable_flags():
    assert LLMGenerationError(errors.AUTH, "x").fatal
    assert not LLMGenerationError(errors.AUTH, "x").retryable
    assert LLMGenerationError(errors.RATE_LIMIT, "x").retryable
    assert not LLMGenerationError(errors.OUTPUT_BUDGET_EXHAUSTED, "x").fatal


def test_looks_truncated():
    assert looks_truncated("```sql\nSELECT a FROM", 600, 600)
    assert not looks_truncated("```sql\nSELECT 1\n```", 600, 600)
    assert not looks_truncated("```sql\nSELECT a FROM", 300, 600)


# ----------------------------------------------------------------------
# output budget growth
# ----------------------------------------------------------------------

def test_empty_output_grows_budget_then_succeeds():
    gen, client = make([LLMClientError("OpenRouter returned empty content."), GOOD], max_output_tokens=1000)
    out = gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert out.sql == "SELECT email FROM customers"
    assert [c["max_tokens"] for c in client.calls] == [1000, 2000]
    assert out.budget_retries == 1 and out.max_output_tokens == 2000


def test_truncated_output_grows_budget():
    truncated = FakeResp("```sql\nSELECT email FROM cust", completion_tokens=1000)
    gen, client = make([truncated, GOOD], max_output_tokens=1000)
    out = gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert out.budget_retries == 1
    assert [c["max_tokens"] for c in client.calls] == [1000, 2000]


def test_budget_exhausted_raises_after_cap():
    empty = LLMClientError("OpenRouter returned empty content.")
    gen, client = make([empty] * 10, max_output_tokens=4096)
    with pytest.raises(LLMGenerationError) as ei:
        gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert ei.value.kind == errors.OUTPUT_BUDGET_EXHAUSTED
    assert [c["max_tokens"] for c in client.calls] == [4096, 8192, 16384]
    assert ei.value.max_output_tokens == 16384


def test_reply_without_sql_counts_as_empty():
    gen, _ = make([FakeResp("I cannot help with that."), GOOD], max_output_tokens=1000)
    out = gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert out.budget_retries == 1


# ----------------------------------------------------------------------
# context length shrinking
# ----------------------------------------------------------------------

def test_context_length_drops_insight_then_exemplars():
    ctx = LLMClientError("HTTP 400 for u. Body: maximum context length exceeded")
    gen, client = make([ctx, ctx, GOOD])
    ex = [SchemaExemplar("e", "Q", "SELECT 1", ["customers"], [], "TABLE customers", "t")]
    ins = SchemaInsight(table_frequency={"customers": 3}, sample_count=3)
    out = gen.draft(TASK, schema_text="s", exemplars=ex, insight=ins)
    assert out.context_shrinks == 2
    lens = [c["prompt_len"] for c in client.calls]
    assert lens[0] > lens[1] > lens[2]


def test_context_length_on_bare_prompt_raises():
    ctx = LLMClientError("HTTP 413 for u. Body: prompt is too long")
    gen, _ = make([ctx])
    with pytest.raises(LLMGenerationError) as ei:
        gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert ei.value.kind == errors.CONTEXT_LENGTH


# ----------------------------------------------------------------------
# fatal / retryable pass-through
# ----------------------------------------------------------------------

def test_auth_error_is_fatal_and_not_retried():
    gen, client = make([LLMClientError("HTTP 401 for u. Body: bad key")])
    with pytest.raises(LLMGenerationError) as ei:
        gen.draft(TASK, schema_text="s", exemplars=[], insight=None)
    assert ei.value.fatal and len(client.calls) == 1


# ----------------------------------------------------------------------
# runner policy
# ----------------------------------------------------------------------

def test_runner_records_retryable_error_and_aborts_on_fatal(tmp_path):
    from democase_sql.db.build_db import build
    from democase_sql.env import SqlEnvironment
    from democase_sql.runner import run_condition
    from democase_sql.tasks import build_tasks

    db = build(tmp_path / "d.sqlite")
    env = SqlEnvironment(db)
    tasks = build_tasks(db)[:4]

    # 1 transport error (recorded, continue), then good, then auth (abort)
    gen, _ = make(
        [LLMClientError("ConnectionError: refused"), GOOD, LLMClientError("HTTP 401 for u. Body: nope")]
    )
    with pytest.raises(RunAborted) as ei:
        run_condition("pure_dynamic", tasks, env=env, generator=gen, output_dir=tmp_path)
    assert "fatal LLM error" in str(ei.value)
    assert (tmp_path / "pure_dynamic" / "episodes.json").exists()  # partial results persisted


def test_runner_aborts_after_consecutive_errors(tmp_path):
    from democase_sql.db.build_db import build
    from democase_sql.env import SqlEnvironment
    from democase_sql.runner import run_condition
    from democase_sql.tasks import build_tasks

    db = build(tmp_path / "d.sqlite")
    env = SqlEnvironment(db)
    tasks = build_tasks(db)[:6]
    gen, client = make([LLMClientError("HTTP 503 for u. Body: down")] * 6)
    with pytest.raises(RunAborted) as ei:
        run_condition("pure_dynamic", tasks, env=env, generator=gen, output_dir=tmp_path, max_consecutive_errors=3)
    assert "3 consecutive errors" in str(ei.value)
    assert len(client.calls) == 3


def test_env_rejects_multiple_statements_and_empty(tmp_path):
    from democase_sql.db.build_db import build
    from democase_sql.env import SqlEnvironment

    env = SqlEnvironment(build(tmp_path / "d.sqlite"))
    assert env.execute("")["error"] == "Empty SQL."
    r = env.execute("SELECT 1; SELECT 2")
    assert not r["ok"] and "one statement" in r["error"]
    assert env.execute("SELECT 1;")["ok"]
