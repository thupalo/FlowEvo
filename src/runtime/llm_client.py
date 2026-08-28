"""HTTP client for experiment LLM calls (OpenAI-compatible chat completions)."""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

import requests

from runtime.config import GenerationSettings, RuntimeLLMConfig


DEFAULT_SYSTEM_INSTRUCTIONS = "You are a careful and precise assistant."


class LLMClientError(RuntimeError):
    """Raised when the LLM provider fails or returns invalid content.

    Attributes:
        status_code: HTTP status when the failure came from a non-2xx reply,
            otherwise ``None`` (transport error, empty content, bad JSON).
        finish_reason: the provider's ``finish_reason`` when the failure is an
            empty reply (e.g. ``"length"`` when a reasoning model exhausted
            ``max_tokens`` while thinking), otherwise ``""``.
    """

    def __init__(self, message: str, *, status_code: int | None = None, finish_reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.finish_reason = finish_reason


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    finish_reason: str = ""
    reasoning: str = ""


class LLMClient:
    """Small wrapper around the active experiment LLM provider."""

    CONNECT_TIMEOUT_SECONDS = 15.0
    REQUEST_TIMEOUT_SECONDS = 90.0
    MAX_RETRIES = 4
    RETRY_BACKOFF_SECONDS = 2.0
    MAX_RETRY_AFTER_SECONDS = 30.0
    # Output-budget growth (only when config.grow_on_truncation is True)
    MAX_BUDGET_GROWTH_STEPS = 3

    def __init__(self, config: RuntimeLLMConfig) -> None:
        self.config = config
        from runtime.config import SUPPORTED_PROVIDERS
        if config.provider not in SUPPORTED_PROVIDERS:
            raise LLMClientError(
                "Unsupported provider `%s`. Must be one of %s." % (config.provider, sorted(SUPPORTED_PROVIDERS))
            )

    def _sanitize(self, text: str) -> str:
        sanitized = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED_API_KEY]", text)
        sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED_TOKEN]", sanitized)
        sanitized = re.sub(r"eyJ[A-Za-z0-9._-]+", "[REDACTED_TOKEN]", sanitized)
        return sanitized[:600]

    def _usage_int(self, usage: object, field: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(field, 0) or 0)
        return int(getattr(usage, field, 0) or 0)

    def _is_transient_transport_error(self, exc: Exception) -> bool:
        transient_types = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
            requests.exceptions.ChunkedEncodingError,
        )
        return isinstance(exc, transient_types)

    def _is_transient_status(self, status_code: int) -> bool:
        return status_code in {408, 409, 425, 429} or 500 <= status_code < 600

    def _retry_after_seconds(self, response: requests.Response) -> float | None:
        header_value = str(response.headers.get("retry-after", "") or "").strip()
        if not header_value:
            return None
        try:
            parsed = float(header_value)
        except ValueError:
            return None
        return max(0.0, min(parsed, self.MAX_RETRY_AFTER_SECONDS))

    def _retry_backoff(self, retry_index: int, *, response: requests.Response | None = None) -> None:
        retry_after = self._retry_after_seconds(response) if response is not None else None
        base_delay = retry_after if retry_after is not None else self.RETRY_BACKOFF_SECONDS * (2 ** max(retry_index - 1, 0))
        jitter = 0.2 * random.random() * max(base_delay, 1.0)
        time.sleep(min(base_delay + jitter, self.MAX_RETRY_AFTER_SECONDS))

    def _request_timeout(self) -> tuple[float, float]:
        return (self.CONNECT_TIMEOUT_SECONDS, self.REQUEST_TIMEOUT_SECONDS)

    def generate(self, *, instructions: str, input_text: str, settings: GenerationSettings) -> LLMResponse:
        if self.config.provider == "openrouter":
            return self._generate_openrouter(
                instructions=instructions,
                input_text=input_text,
                settings=settings,
            )
        raise LLMClientError(
            "Unsupported provider `%s` at generation time." % self.config.provider
        )

    # ------------------------------------------------------------------
    # OpenRouter (OpenAI Chat Completions compatible)
    # ------------------------------------------------------------------

    def _post_with_retries(self, url: str, headers: dict[str, str], payload: dict[str, object]) -> dict:
        """POST once, retrying transient transport errors / statuses.

        Returns the parsed JSON body. Raises ``LLMClientError`` carrying the
        HTTP status for non-transient failures.
        """
        transient_retry_count = 0
        last_error = "retry budget exhausted"
        last_status: int | None = None

        for _attempt in range(self.MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url, headers=headers, json=payload,
                    timeout=self._request_timeout(),
                )
            except Exception as exc:  # noqa: BLE001
                last_error = self._sanitize("%s: %s" % (type(exc).__name__, exc))
                last_status = None
                if self._is_transient_transport_error(exc) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count)
                    continue
                raise LLMClientError(last_error) from exc

            if not response.ok:
                last_status = int(response.status_code)
                last_error = self._sanitize(
                    "HTTP %d for %s. Body: %s" % (response.status_code, url, response.text[:500])
                )
                if self._is_transient_status(response.status_code) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count, response=response)
                    continue
                raise LLMClientError(last_error, status_code=last_status)

            try:
                return response.json()
            except Exception as exc:  # noqa: BLE001
                raise LLMClientError(self._sanitize("Invalid JSON: %s" % response.text[:200])) from exc

        raise LLMClientError(
            self._sanitize(
                "Exceeded retry limit after %d transient retries. Last error: %s"
                % (transient_retry_count, last_error)
            ),
            status_code=last_status,
        )

    def _generate_openrouter(
        self, *, instructions: str, input_text: str, settings: GenerationSettings,
    ) -> LLMResponse:
        effective_instructions = (instructions.strip() or DEFAULT_SYSTEM_INSTRUCTIONS)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": effective_instructions},
            {"role": "user", "content": input_text},
        ]
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": settings.temperature,
            "max_tokens": settings.max_output_tokens,
        }
        url = "%s/chat/completions" % self.config.base_url.rstrip("/")
        headers = {
            "Authorization": "Bearer %s" % self.config.api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/DEFENSE-SEU/FlowEvo",
            "X-Title": self.config.app_name or "FlowEvo",
        }
        started = time.perf_counter()

        grow = bool(getattr(self.config, "grow_on_truncation", False))
        cap = int(getattr(self.config, "max_output_tokens_cap", 16384) or 16384)
        max_tokens = int(settings.max_output_tokens)
        growth_steps = 0

        while True:
            payload["max_tokens"] = max_tokens
            data = self._post_with_retries(url, headers, payload)

            choices = data.get("choices") or []
            text = ""
            finish_reason = ""
            reasoning = ""
            if choices:
                choice = choices[0] or {}
                finish_reason = str(choice.get("finish_reason") or "")
                message = choice.get("message") or {}
                text = str(message.get("content") or "").strip()
                # Reasoning models (vLLM, DeepSeek, OpenRouter) expose the
                # hidden chain of thought under one of these keys.
                reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")

            # finish_reason == "length" means the reply is incomplete, whether
            # content is empty (a reasoning model spent the budget thinking)
            # or cut off mid-answer. With growth enabled, retry with a bigger
            # budget in both cases; otherwise return what we have (callers can
            # inspect finish_reason) or raise when there is nothing at all.
            can_grow = (
                grow
                and finish_reason == "length"
                and growth_steps < self.MAX_BUDGET_GROWTH_STEPS
                and max_tokens < cap
            )
            if can_grow:
                max_tokens = min(max_tokens * 2, cap)
                growth_steps += 1
                continue

            if text:
                usage = data.get("usage") or {}
                latency_ms = (time.perf_counter() - started) * 1000.0
                return LLMResponse(
                    text=text,
                    provider=self.config.provider,
                    model=self.config.model,
                    prompt_tokens=self._usage_int(usage, "prompt_tokens"),
                    completion_tokens=self._usage_int(usage, "completion_tokens"),
                    total_tokens=self._usage_int(usage, "total_tokens"),
                    latency_ms=latency_ms,
                    finish_reason=finish_reason,
                    reasoning=reasoning,
                )

            detail = ""
            if finish_reason:
                detail += " finish_reason=%s" % finish_reason
            if reasoning:
                detail += " reasoning_chars=%d" % len(reasoning)
            if growth_steps:
                detail += " after %d budget increases (max_tokens=%d)" % (growth_steps, max_tokens)
            raise LLMClientError(
                "OpenRouter returned empty content.%s" % detail,
                finish_reason=finish_reason,
            )
