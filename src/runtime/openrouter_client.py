"""Compatibility shim for older OpenRouter-specific imports."""

from runtime.llm_client import LLMClient, LLMClientError, LLMResponse, OpenAI

OpenRouterClient = LLMClient

__all__ = ["LLMClient", "LLMClientError", "LLMResponse", "OpenAI", "OpenRouterClient"]
