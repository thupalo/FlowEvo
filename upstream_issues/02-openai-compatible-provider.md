# Runtime: "openrouter" provider is really "any OpenAI-compatible endpoint" — make that explicit

## Summary

The runtime works with any OpenAI-compatible chat-completions server (vLLM, llama.cpp, LM Studio, Ollama, Azure proxies) by changing `base_url`, but nothing in the code or docs says so: the only accepted provider name is `openrouter` (`src/runtime/config.py:17`), OpenRouter-specific headers are always sent (`src/runtime/llm_client.py:123-126`), and the config error message says "OpenRouter provider requires an api_key". Users with local models assume they are unsupported.

## Observed

```python
SUPPORTED_PROVIDERS = {"openrouter"}
...
headers = {
    "Authorization": "Bearer %s" % self.config.api_key,
    "HTTP-Referer": "https://github.com/DEFENSE-SEU/FlowEvo",
    "X-Title": self.config.app_name or "FlowEvo",
}
```

Local servers accept the extra headers silently, so it works — but only by accident of reading the source.

## Proposed change

1. Accept `openai_compatible` (and keep `openrouter` as an alias) in `SUPPORTED_PROVIDERS`; route both through `_generate_openrouter` (rename to `_generate_openai_chat`).
2. Send `HTTP-Referer` / `X-Title` only when `base_url` contains `openrouter.ai`.
3. Make `api_key` optional for non-OpenRouter endpoints (many local servers ignore it); keep the hard requirement for `openrouter.ai`.
4. README "Configuring the LLM backend": add a three-line local-model recipe:

```yaml
llm:
  base_url: http://localhost:8000/v1
  model: <served model name>
  api_key: "not-needed"
```

## Scope / reproducibility risk

~15 lines + docs. No behavioural change for OpenRouter users (`provider: openrouter` with the hosted endpoint still requires the key and still sends the attribution headers).

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `fc0a045` (code; README recipe in `f62f5b9`): `SUPPORTED_PROVIDERS = {"openrouter", "openai_compatible"}`, `is_openrouter_endpoint(base_url)`, `_request_headers()` sends `Authorization` only when a key is set and the attribution headers only for `openrouter.ai`, `_generate_openrouter` renamed `_generate_openai_chat`; config tests cover local-without-key, alias-with-custom-URL, and hosted-requires-key.

## Related

- Reasoning-model handling (separate issue) — most local models people will plug in are reasoning models.
