"""Codex-backed runtime client for experiment LLM calls."""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from runtime.config import GenerationSettings, RuntimeLLMConfig


DEFAULT_CODEX_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. "
    "You are running as a coding agent in a local experiment harness on a user's computer."
)


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


@dataclass(frozen=True)
class CodexAuthState:
    token: str
    account_id: str
    auth_mode: str
    has_refresh_token: bool
    last_refresh: str
    path: Path


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

    def _codex_auth_guidance(self, *, path: Path, auth_mode: str, has_refresh_token: bool, last_refresh: str) -> str:
        details = [
            "openai-codex reuses an existing Codex login cache.",
            "Run `codex` or `codex login` to refresh the session.",
            "Set `cli_auth_credentials_store = \"file\"` if Codex is storing credentials in keyring.",
            f"Verify `{path}` exists and includes `.tokens.refresh_token`.",
        ]
        if auth_mode:
            details.append(f"Observed auth_mode={auth_mode}.")
        details.append(f"Observed has_refresh_token={has_refresh_token}.")
        if last_refresh:
            details.append(f"Observed last_refresh={last_refresh}.")
        return " ".join(details)

    def _load_codex_auth_state(self) -> CodexAuthState:
        path = Path(self.config.auth_file_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LLMClientError(
                self._codex_auth_guidance(path=path, auth_mode="", has_refresh_token=False, last_refresh="")
            ) from exc
        except json.JSONDecodeError as exc:
            raise LLMClientError(
                self._sanitize(
                    f"Malformed Codex auth cache at {path}. "
                    + self._codex_auth_guidance(path=path, auth_mode="", has_refresh_token=False, last_refresh="")
                )
            ) from exc

        if not isinstance(data, dict):
            raise LLMClientError(
                self._sanitize(
                    f"Malformed Codex auth cache at {path}. Expected a JSON object. "
                    + self._codex_auth_guidance(path=path, auth_mode="", has_refresh_token=False, last_refresh="")
                )
            )

        tokens = data.get("tokens")
        auth_mode = str(data.get("auth_mode", "") or "").strip()
        last_refresh = str(data.get("last_refresh", "") or "").strip()
        account_id = ""
        has_refresh_token = False
        candidates: list[str] = []

        def maybe_add(value: object) -> None:
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

        if isinstance(tokens, dict):
            maybe_add(tokens.get("access_token"))
            maybe_add(tokens.get("id_token"))
            account_id = str(tokens.get("account_id", "") or "").strip()
            has_refresh_token = isinstance(tokens.get("refresh_token"), str) and bool(tokens.get("refresh_token").strip())

        maybe_add(data.get("access_token"))
        maybe_add(data.get("token"))
        for key in ("auth", "credentials", "session"):
            value = data.get(key)
            if isinstance(value, dict):
                maybe_add(value.get("access_token"))
                maybe_add(value.get("token"))

        if not candidates:
            raise LLMClientError(
                self._sanitize(
                    f"Could not find a usable token in {path}. "
                    + self._codex_auth_guidance(
                        path=path,
                        auth_mode=auth_mode,
                        has_refresh_token=has_refresh_token,
                        last_refresh=last_refresh,
                    )
                )
            )

        return CodexAuthState(
            token=candidates[0],
            account_id=account_id,
            auth_mode=auth_mode,
            has_refresh_token=has_refresh_token,
            last_refresh=last_refresh,
            path=path,
        )

    def _responses_input(self, *, instructions: str, input_text: str) -> list[dict[str, Any]]:
        effective_instructions = instructions.strip() or DEFAULT_CODEX_INSTRUCTIONS
        return [
            {"role": "system", "content": [{"type": "input_text", "text": effective_instructions}]},
            {"role": "user", "content": [{"type": "input_text", "text": input_text}]},
        ]

    def _extract_output_text(self, payload: object) -> str:
        if not isinstance(payload, dict):
            return ""
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        texts: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if content.get("type") in {"output_text", "text"} and isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        return "\n".join(texts).strip()

    def _iter_sse_data(self, response: requests.Response) -> Iterable[str]:
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            yield data

    def _codex_payload(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: GenerationSettings,
        include_temperature: bool = True,
        include_max_output_tokens: bool = True,
    ) -> dict[str, Any]:
        effective_instructions = instructions.strip() or DEFAULT_CODEX_INSTRUCTIONS
        payload = {
            "model": self.config.model,
            "input": self._responses_input(instructions=effective_instructions, input_text=input_text),
            "instructions": effective_instructions,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"summary": "auto"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if include_temperature:
            payload["temperature"] = settings.temperature
        if include_max_output_tokens:
            payload["max_output_tokens"] = settings.max_output_tokens
        return payload

    def _unsupported_payload_parameter(self, response_text: str) -> str:
        match = re.search(r"Unsupported parameter:\s*([A-Za-z0-9_]+)", response_text)
        return match.group(1) if match else ""

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

    def _raise_codex_401(self, state: CodexAuthState, detail: str) -> None:
        message = self._codex_auth_guidance(
            path=state.path,
            auth_mode=state.auth_mode,
            has_refresh_token=state.has_refresh_token,
            last_refresh=state.last_refresh,
        )
        raise LLMClientError(self._sanitize(f"Codex backend returned 401. {message} Body: {detail[:500]}"))

    def _request_timeout(self) -> tuple[float, float]:
        return (self.CONNECT_TIMEOUT_SECONDS, self.REQUEST_TIMEOUT_SECONDS)

    def _post_codex_stream(self, state: CodexAuthState, payload: dict[str, Any]) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {state.token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": self.config.app_name or "improving_agent",
        }
        if state.account_id:
            headers["ChatGPT-Account-Id"] = state.account_id
        return requests.post(
            f"{self.config.base_url.rstrip('/')}/responses",
            headers=headers,
            json=payload,
            timeout=self._request_timeout(),
            stream=True,
        )

    def _parse_codex_stream(self, response: requests.Response) -> tuple[str, dict[str, Any], str]:
        chunks: list[str] = []
        completed_response: dict[str, Any] = {}
        last_event_type = ""
        for item in self._iter_sse_data(response):
            try:
                event = json.loads(item)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type", "") or "")
            if event_type:
                last_event_type = event_type
            if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                chunks.append(event["delta"])
                continue
            if event_type == "response.output_text.done" and isinstance(event.get("text"), str):
                if not chunks:
                    chunks.append(event["text"])
                continue
            if event_type == "response.completed" and isinstance(event.get("response"), dict):
                completed_response = event["response"]

        text = "".join(chunks).strip()
        if not text:
            text = self._extract_output_text(completed_response)
        return text, completed_response, last_event_type

    def _generate_openai_codex(self, *, instructions: str, input_text: str, settings: GenerationSettings) -> LLMResponse:
        include_temperature = True
        include_max_output_tokens = True
        payload = self._codex_payload(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            include_temperature=include_temperature,
            include_max_output_tokens=include_max_output_tokens,
        )
        started = time.perf_counter()
        state = self._load_codex_auth_state()
        transient_retry_count = 0
        last_error = "retry budget exhausted"
        max_attempts = max(6, self.MAX_RETRIES + 4)
        for attempt in range(max_attempts):
            try:
                response = self._post_codex_stream(state, payload)
            except Exception as exc:  # noqa: BLE001
                last_error = self._sanitize(f"{type(exc).__name__}: {exc}")
                if self._is_transient_transport_error(exc) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count)
                    continue
                raise LLMClientError(last_error) from exc

            if response.status_code == 401:
                last_error = self._sanitize(f"HTTP 401 for {self.config.base_url.rstrip('/')}/responses. Body: {response.text[:1000]}")
                if attempt == 0:
                    state = self._load_codex_auth_state()
                    continue
                self._raise_codex_401(state, response.text)
            if not response.ok:
                last_error = self._sanitize(
                    f"HTTP {response.status_code} for {self.config.base_url.rstrip('/')}/responses. Body: {response.text[:1000]}"
                )
                if self._is_transient_status(response.status_code) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count, response=response)
                    continue
                unsupported = self._unsupported_payload_parameter(response.text)
                if unsupported == "temperature" and include_temperature:
                    include_temperature = False
                    payload = self._codex_payload(
                        instructions=instructions,
                        input_text=input_text,
                        settings=settings,
                        include_temperature=include_temperature,
                        include_max_output_tokens=include_max_output_tokens,
                    )
                    continue
                if unsupported == "max_output_tokens" and include_max_output_tokens:
                    include_max_output_tokens = False
                    payload = self._codex_payload(
                        instructions=instructions,
                        input_text=input_text,
                        settings=settings,
                        include_temperature=include_temperature,
                        include_max_output_tokens=include_max_output_tokens,
                    )
                    continue
                raise LLMClientError(last_error)

            try:
                text, completed_response, last_event_type = self._parse_codex_stream(response)
            except Exception as exc:  # noqa: BLE001
                last_error = self._sanitize(f"{type(exc).__name__}: {exc}")
                if self._is_transient_transport_error(exc) and transient_retry_count < self.MAX_RETRIES:
                    transient_retry_count += 1
                    self._retry_backoff(transient_retry_count)
                    continue
                raise LLMClientError(last_error) from exc
            if not text:
                last_error = self._sanitize(
                    "Codex backend stream completed without output text. "
                    f"Last event type: {last_event_type or 'unknown'}."
                )
                raise LLMClientError(
                    last_error
                )

            latency_ms = (time.perf_counter() - started) * 1000.0
            usage = {}
            if isinstance(completed_response, dict):
                maybe_usage = completed_response.get("usage")
                if isinstance(maybe_usage, dict):
                    usage = maybe_usage
            return LLMResponse(
                text=text,
                provider=self.config.provider,
                model=self.config.model,
                prompt_tokens=self._usage_int(usage, "input_tokens"),
                completion_tokens=self._usage_int(usage, "output_tokens"),
                total_tokens=self._usage_int(usage, "total_tokens"),
                latency_ms=latency_ms,
            )

        raise LLMClientError(
            self._sanitize(
                f"Exceeded retry limit after {transient_retry_count} transient retries. Last error: {last_error}"
            )
        )

    def generate(self, *, instructions: str, input_text: str, settings: GenerationSettings) -> LLMResponse:
        if self.config.provider == "openrouter":
            return self._generate_openrouter(
                instructions=instructions,
                input_text=input_text,
                settings=settings,
            )
        return self._generate_openai_codex(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
        )

    # ------------------------------------------------------------------
    # OpenRouter (OpenAI Chat Completions compatible)
    # ------------------------------------------------------------------

    def _generate_openrouter(
        self, *, instructions: str, input_text: str, settings: GenerationSettings,
    ) -> LLMResponse:
        effective_instructions = (instructions.strip() or DEFAULT_CODEX_INSTRUCTIONS)
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
            "HTTP-Referer": "https://github.com/FlowEvo",
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


class OpenAI:  # pragma: no cover - compatibility-only export for older imports
    pass
