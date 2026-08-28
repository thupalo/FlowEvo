from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime import errors  # noqa: E402
from runtime.errors import LLMGenerationError, classify_client_error  # noqa: E402
from runtime.llm_client import LLMClientError  # noqa: E402


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
def test_classify_from_message(msg, kind, status):
    assert classify_client_error(LLMClientError(msg)) == (kind, status)


def test_classify_prefers_structured_attributes():
    exc = LLMClientError("HTTP 400 for u. Body: whatever", status_code=401)
    assert classify_client_error(exc) == (errors.AUTH, 401)
    exc = LLMClientError("OpenRouter returned empty content. finish_reason=length", finish_reason="length")
    assert classify_client_error(exc) == (errors.OUTPUT_TRUNCATED, None)
    exc = LLMClientError(
        "OpenRouter returned empty content. finish_reason=length after 3 budget increases (max_tokens=16384)",
        finish_reason="length",
    )
    assert classify_client_error(exc) == (errors.OUTPUT_BUDGET_EXHAUSTED, None)


def test_generation_error_flags_and_str():
    e = LLMGenerationError.from_client_error(LLMClientError("HTTP 401 for u. Body: x"), max_output_tokens=900)
    assert e.fatal and not e.retryable and e.http_status == 401
    assert str(e).startswith("[auth]") and "max_output_tokens=900" in str(e)
    assert isinstance(e, RuntimeError)
    r = LLMGenerationError(errors.RATE_LIMIT, "x")
    assert r.retryable and not r.fatal
