"""Episode-loop error policy of the code/math runner, with a scripted LLM."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from code_math import runner as cm  # noqa: E402
from core.schemas import CodeTaskInstance  # noqa: E402
from runtime.errors import RunAborted  # noqa: E402
from runtime.llm_client import LLMClientError  # noqa: E402


@dataclass
class Resp:
    text: str
    prompt_tokens: int = 10
    completion_tokens: int = 5


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate(self, *, instructions, input_text, settings):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tasks(n: int) -> list[CodeTaskInstance]:
    return [
        CodeTaskInstance(
            task_id="t%d" % i,
            benchmark="humaneval",
            prompt="def add(a, b):\n    \"\"\"Return a + b.\"\"\"\n",
            entry_point="add",
            test="def check(f):\n    assert f(1, 2) == 3\n",
        )
        for i in range(n)
    ]


def test_fatal_error_aborts_and_persists_checkpoint(tmp_path):
    llm = ScriptedLLM([Resp("```python\ndef add(a, b):\n    return a + b\n```"), LLMClientError("HTTP 401 for u. Body: nope", status_code=401)])
    with pytest.raises(RunAborted) as ei:
        cm.run_condition("humaneval", "io_baseline", cm.CONDITIONS["io_baseline"], llm, tasks(3), output_dir=tmp_path)
    assert "fatal LLM error (auth)" in str(ei.value)
    ckpt = tmp_path / "_checkpoint_humaneval_io_baseline.json"
    assert ckpt.exists()
    import json

    eps = json.loads(ckpt.read_text(encoding="utf-8"))["episodes"]
    assert [e["failure_type"] for e in eps] == ["", "auth"]


def test_consecutive_retryable_errors_abort(tmp_path):
    llm = ScriptedLLM([LLMClientError("HTTP 503 for u. Body: down", status_code=503)] * 5)
    with pytest.raises(RunAborted) as ei:
        cm.run_condition("humaneval", "io_baseline", cm.CONDITIONS["io_baseline"], llm, tasks(5), output_dir=tmp_path)
    assert "3 consecutive errors" in str(ei.value)
    assert llm.calls == 3


def test_isolated_retryable_error_is_recorded_and_run_continues(tmp_path):
    good = Resp("```python\ndef add(a, b):\n    return a + b\n```")
    llm = ScriptedLLM([good, LLMClientError("ConnectionError: refused"), good])
    eps = cm.run_condition("humaneval", "io_baseline", cm.CONDITIONS["io_baseline"], llm, tasks(3), output_dir=tmp_path)
    assert [e["passed"] for e in eps] == [True, False, True]
    assert eps[1]["failure_type"] == "transport"
    assert cm._summary(eps)["failure_types"] == {"transport": 1}


def test_prose_reply_is_recorded_as_empty_output(tmp_path):
    llm = ScriptedLLM([Resp("I cannot help with that.")])
    eps = cm.run_condition("humaneval", "io_baseline", cm.CONDITIONS["io_baseline"], llm, tasks(1), output_dir=tmp_path)
    assert eps[0]["passed"] is False and eps[0]["failure_type"] == "empty_output"
