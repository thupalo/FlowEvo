"""Typed classification of LLM runtime failures.

``LLMClient`` raises a single ``LLMClientError``; this module turns it into an
explicit *kind* so callers can choose between: grow the output budget, shrink
the prompt, retry later, skip the episode, or abort the run.

Typical use in an experiment runner::

    try:
        resp = client.generate(...)
    except LLMClientError as exc:
        err = LLMGenerationError.from_client_error(exc)
        if err.fatal:
            raise RunAborted(str(err)) from exc
        episode["failure_type"] = err.kind
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class RunAborted(RuntimeError):
    """An experiment loop stopped early (fatal error or too many consecutive failures)."""


# Failure kinds, roughly ordered by how the caller should react.
EMPTY_OUTPUT = "empty_output"                  # content empty                    -> grow max_tokens
OUTPUT_TRUNCATED = "output_truncated"          # content cut off at max_tokens    -> grow max_tokens
OUTPUT_BUDGET_EXHAUSTED = "output_budget_exhausted"  # grew to the cap, still nothing
CONTEXT_LENGTH = "context_length"              # prompt too long                  -> shrink prompt
RATE_LIMIT = "rate_limit"                      # 429 after the client's retries   -> retry later
SERVER = "server_error"                        # 5xx after retries                -> retry later
TRANSPORT = "transport"                        # connection / timeout             -> retry later
AUTH = "auth"                                  # 401 / 403                        -> fatal
BAD_REQUEST = "bad_request"                    # other 4xx (bad model name …)     -> fatal
MALFORMED = "malformed_response"               # invalid JSON / unexpected shape  -> retry later
UNKNOWN = "unknown"

FATAL_KINDS = frozenset({AUTH, BAD_REQUEST})
RETRYABLE_KINDS = frozenset({RATE_LIMIT, SERVER, TRANSPORT, MALFORMED})

_HTTP_RE = re.compile(r"HTTP (\d{3})")
_CONTEXT_RE = re.compile(
    r"context[_ ]length|maximum context|max(?:imum)?[_ ]tokens.*(?:exceed|limit)|too many tokens|"
    r"prompt is too long|input.*too long|reduce the length|exceeds the model",
    re.IGNORECASE,
)
_TRANSPORT_TOKENS = ("ConnectionError", "Timeout", "SSLError", "ChunkedEncodingError", "Exceeded retry limit")


def classify_client_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an ``LLMClientError`` (or any exception) to ``(kind, http_status)``.

    Uses the structured ``status_code`` / ``finish_reason`` attributes when
    the exception carries them, and falls back to parsing the message.
    """
    text = str(exc)
    status = getattr(exc, "status_code", None)
    if status is None:
        m = _HTTP_RE.search(text)
        status = int(m.group(1)) if m else None

    if "empty content" in text:
        finish_reason = str(getattr(exc, "finish_reason", "") or "")
        if finish_reason == "length" and "budget increases" in text:
            return OUTPUT_BUDGET_EXHAUSTED, None
        return (OUTPUT_TRUNCATED if finish_reason == "length" else EMPTY_OUTPUT), None
    if status is not None:
        if status in (401, 403):
            return AUTH, status
        if status == 429:
            return RATE_LIMIT, status
        if status >= 500:
            return SERVER, status
        if status in (400, 413, 422) and _CONTEXT_RE.search(text):
            return CONTEXT_LENGTH, status
        return BAD_REQUEST, status
    if _CONTEXT_RE.search(text):
        return CONTEXT_LENGTH, None
    if "Invalid JSON" in text:
        return MALFORMED, None
    if any(tok in text for tok in _TRANSPORT_TOKENS):
        return TRANSPORT, None
    return UNKNOWN, None


@dataclass
class LLMGenerationError(RuntimeError):
    """A classified LLM failure."""

    kind: str
    message: str
    http_status: int | None = None
    max_output_tokens: int = 0

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, str(self))

    def __str__(self) -> str:
        extra = " (max_output_tokens=%d)" % self.max_output_tokens if self.max_output_tokens else ""
        return "[%s] %s%s" % (self.kind, self.message, extra)

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL_KINDS

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @classmethod
    def from_client_error(cls, exc: BaseException, *, max_output_tokens: int = 0) -> "LLMGenerationError":
        kind, status = classify_client_error(exc)
        return cls(kind=kind, message=str(exc), http_status=status, max_output_tokens=max_output_tokens)
