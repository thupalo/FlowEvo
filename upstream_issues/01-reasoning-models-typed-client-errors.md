# Runtime: support reasoning models and expose typed LLM client errors

## Summary

`runtime/llm_client.py` reads only `message.content`, discards the `reasoning` field, ignores `finish_reason`, and folds every failure into a single `LLMClientError` whose only information is the message string. With any *reasoning* backbone (Nemotron 3.5, DeepSeek-R1, Qwen-thinking, o-series behind a proxy) the hidden thinking consumes `max_tokens`, `content` comes back empty, and the client raises — for every consumer of the runtime. Callers also cannot distinguish a context-length 400 from an unknown-model 400, or a truncated answer from a wrong one.

## Environment

- FlowEvo `main` @ `36e81ef`
- Backbone: `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` behind a vLLM-style OpenAI-compatible server, configured through `configs/local.yaml` (`base_url`, `model`, `api_key`)
- Same behaviour expected with any model that returns `reasoning` / `reasoning_content`

## Steps to reproduce

1. Point `configs/local.yaml` at a reasoning model.
2. Call `LLMClient.generate(...)` with `GenerationSettings(temperature=0, max_output_tokens=600)` on any non-trivial prompt (the default `draft.max_output_tokens` is 900).

## Observed

Raw server reply:

```
finish_reason: length
usage: {prompt_tokens: 328, completion_tokens: 600}
message.content: ""
message.reasoning: "Here's a thinking process:\n\n1. **Analyze the User's Request:** ..."   (2.5 kB)
```

Client:

```
runtime.llm_client.LLMClientError: OpenRouter returned empty content.
```

(`src/runtime/llm_client.py:167`). With `max_tokens=4000` the same prompt succeeds in 654 completion tokens.

## Expected

- The response object carries `finish_reason` and `reasoning` so callers can tell "budget exhausted while thinking" from "empty answer".
- Failures are classifiable without parsing message text (HTTP status, kind).
- Optionally, the client can grow the output budget on truncation.

## Proposed change (three small PRs)

**PR 1 — additive response/error fields** (`src/runtime/llm_client.py`)

```python
@dataclass(frozen=True)
class LLMResponse:
    ...
    finish_reason: str = ""
    reasoning: str = ""

class LLMClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None: ...
```

Populate from `choices[0].finish_reason` and `message.reasoning` / `message.reasoning_content`. No behaviour change.

**PR 2 — `src/runtime/errors.py`**: classify failures into kinds `empty_output`, `output_truncated`, `context_length`, `rate_limit`, `server_error`, `transport`, `auth`, `bad_request`, `malformed_response`, with `fatal` / `retryable` flags. A tested implementation exists in the fork: `thupalo/FlowEvo` → `democase_sql/errors.py` (+ 21 tests using a scripted fake client).

**PR 3 — opt-in budget growth**: `LLMClient.generate(..., grow_on_truncation: bool = False)`; when `finish_reason == "length"` and content is empty, retry with `max_tokens` doubled, capped (e.g. 16384), at most 2–3 times. Expose as `llm.grow_on_truncation` in the YAML config. **Default off** so published experiment numbers are unchanged.

## Reproducibility risk

PR 1 and PR 2: none (additive). PR 3: none while the flag is off; document that enabling it changes token accounting.

## Related

- Hard-coded small budgets in ALFWorld (`alfworld_/generator.py:52` = 256, `alfworld_/compiler.py:233` = 500) make that runner unusable with reasoning models even after this fix — filed separately.
- Fork reference implementation and evidence: `thupalo/FlowEvo`, `FORK_GUIDE.md` Part C entries C-2026-08-25-a/b.
