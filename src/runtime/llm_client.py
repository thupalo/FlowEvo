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
    """Raised when the LLM provider fails or returns invalid content."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float


class LLMClient:
    """Small wrapper around the active experiment LLM provider."""

    CONNECT_TIMEOUT_SECONDS = 15.0
    REQUEST_TIMEOUT_SECONDS = 90.0
    MAX_RETRIES = 4
    RETRY_BACKOFF_SECONDS = 2.0
    MAX_RETRY_AFTER_SECONDS = 30.0

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
        transient_retry_count = 0
        last_error = "retry budget exhausted"

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url, headers=headers, json=payload,
                    timeout=self._request_timeout(),
                )
            except Exception as exc:  # noqa: BLE001
                last_error = self._sanitize("%s: %s" % (type(exc).__name__, exc))
                if self._is_transient_transport_error(exc) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count)
                    continue
                raise LLMClientError(last_error) from exc

            if not response.ok:
                last_error = self._sanitize(
                    "HTTP %d for %s. Body: %s" % (response.status_code, url, response.text[:500])
                )
                if self._is_transient_status(response.status_code) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count, response=response)
                    continue
                raise LLMClientError(last_error)

            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise LLMClientError(self._sanitize("Invalid JSON: %s" % response.text[:200])) from exc

            choices = data.get("choices") or []
            text = ""
            if choices:
                message = choices[0].get("message") or {}
                text = str(message.get("content") or "").strip()
            if not text:
                raise LLMClientError("OpenRouter returned empty content.")

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
            )

        raise LLMClientError(
            self._sanitize(
                "Exceeded retry limit after %d transient retries. Last error: %s"
                % (transient_retry_count, last_error)
            )
        )
