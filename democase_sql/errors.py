"""Typed errors for the SQL demo and classification of FlowEvo runtime errors.

FlowEvo's ``runtime.llm_client.LLMClient`` collapses every failure into a
single ``LLMClientError`` whose *message* carries the detail (HTTP status,
transport exception, empty content …).  This module turns those messages
into explicit categories so callers can decide between: grow the output
budget, shrink the prompt, retry, skip the episode, or abort the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class SqlDemoError(Exception):
    """Base class for all demo errors."""


class ConfigError(SqlDemoError):
    """Runtime configuration is missing or invalid (fatal)."""


class RunAborted(SqlDemoError):
    """The episode loop stopped early (fatal error or too many consecutive failures)."""


# ----------------------------------------------------------------------
# LLM generation errors
# ----------------------------------------------------------------------

# kind values, roughly ordered by how the caller should react
EMPTY_OUTPUT = "empty_output"          # content empty (reasoning ate the budget) -> grow max_tokens
OUTPUT_TRUNCATED = "output_truncated"  # content cut off at max_tokens            -> grow max_tokens
OUTPUT_BUDGET_EXHAUSTED = "output_budget_exhausted"  # grew to the cap, still no usable answer
CONTEXT_LENGTH = "context_length"      # prompt too long for the model            -> shrink prompt
RATE_LIMIT = "rate_limit"              # 429 after the client's own retries       -> retryable
SERVER = "server_error"                # 5xx after retries                        -> retryable
TRANSPORT = "transport"                # connection / timeout after retries       -> retryable
AUTH = "auth"                          # 401 / 403                                -> fatal
BAD_REQUEST = "bad_request"            # other 4xx (wrong model name, bad payload) -> fatal
MALFORMED = "malformed_response"       # invalid JSON / unexpected shape          -> retryable
UNKNOWN = "unknown"

_FATAL_KINDS = frozenset({AUTH, BAD_REQUEST})
_RETRYABLE_KINDS = frozenset({RATE_LIMIT, SERVER, TRANSPORT, MALFORMED})


@dataclass
class LLMGenerationError(SqlDemoError):
    kind: str
    message: str
    max_output_tokens: int = 0
    http_status: int | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        extra = f" (max_output_tokens={self.max_output_tokens})" if self.max_output_tokens else ""
        return f"[{self.kind}] {self.message}{extra}"

    @property
    def fatal(self) -> bool:
        return self.kind in _FATAL_KINDS

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE_KINDS


_HTTP_RE = re.compile(r"HTTP (\d{3})")
_CONTEXT_RE = re.compile(
    r"context[_ ]length|maximum context|max(?:imum)?[_ ]tokens.*(?:exceed|limit)|too many tokens|prompt is too long|input.*too long|"
    r"reduce the length|exceeds the model",
    re.IGNORECASE,
)


def classify_client_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an ``LLMClientError`` (or any exception) to ``(kind, http_status)``."""
    text = str(exc)
    if "empty content" in text:
        return EMPTY_OUTPUT, None
    m = _HTTP_RE.search(text)
    if m:
        status = int(m.group(1))
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
    if any(tok in text for tok in ("ConnectionError", "Timeout", "SSLError", "ChunkedEncodingError", "Exceeded retry limit")):
        return TRANSPORT, None
    return UNKNOWN, None
