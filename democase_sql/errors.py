"""Typed errors for the SQL demo.

The LLM-failure classification now lives in the FlowEvo runtime
(``src/runtime/errors.py``); this module re-exports it so the demo keeps a
single import path and adds the demo-specific ``SqlDemoError`` /
``ConfigError``.
"""

from __future__ import annotations

from . import _SRC  # noqa: F401  (ensures FlowEvo src is importable)
from runtime.errors import (  # noqa: E402
    AUTH,
    BAD_REQUEST,
    CONTEXT_LENGTH,
    EMPTY_OUTPUT,
    FATAL_KINDS,
    MALFORMED,
    OUTPUT_BUDGET_EXHAUSTED,
    OUTPUT_TRUNCATED,
    RATE_LIMIT,
    RETRYABLE_KINDS,
    SERVER,
    TRANSPORT,
    UNKNOWN,
    LLMGenerationError,
    RunAborted,
    classify_client_error,
)

__all__ = [
    "AUTH", "BAD_REQUEST", "CONTEXT_LENGTH", "EMPTY_OUTPUT", "FATAL_KINDS", "MALFORMED",
    "OUTPUT_BUDGET_EXHAUSTED", "OUTPUT_TRUNCATED", "RATE_LIMIT", "RETRYABLE_KINDS", "SERVER",
    "TRANSPORT", "UNKNOWN", "LLMGenerationError", "RunAborted", "classify_client_error",
    "SqlDemoError", "ConfigError",
]


class SqlDemoError(Exception):
    """Base class for demo-specific errors."""


class ConfigError(SqlDemoError):
    """Runtime configuration is missing or invalid (fatal)."""
