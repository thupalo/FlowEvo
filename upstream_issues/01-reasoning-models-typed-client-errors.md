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
    def __init__(self, message: str, *, status_code: int | None = None, finish_reason: str = "") -> None: ...
```

The empty-content message should also carry the detail (`finish_reason=length reasoning_chars=2562`) so plain logs are diagnosable.

Populate from `choices[0].finish_reason` and `message.reasoning` / `message.reasoning_content`. No behaviour change.

**PR 2 — `src/runtime/errors.py`**: classify failures into kinds `empty_output`, `output_truncated`, `output_budget_exhausted`, `context_length`, `rate_limit`, `server_error`, `transport`, `auth`, `bad_request`, `malformed_response`, with `fatal` / `retryable` flags; prefer the structured `status_code` / `finish_reason` attributes from PR 1 and fall back to parsing the message. Also a `RunAborted` exception for experiment loops (see the runners issue).

**PR 3 — opt-in budget growth**: config keys `llm.grow_on_truncation: false` and `llm.max_output_tokens_cap: 16384`. When enabled and the reply has `finish_reason == "length"`, retry with `max_tokens` doubled, at most 3 steps, up to the cap. The trigger must be `finish_reason` alone, **not** "content is empty": while verifying we saw an intermediate budget return *non-empty content cut off mid-answer* with `finish_reason == "length"`; growing only on empty content would have returned that truncated text as a complete answer. **Default off** so published experiment numbers are unchanged.

## Reproducibility risk

PR 1 and PR 2: none (additive). PR 3: none while the flag is off; document that enabling it changes token accounting.

## Reference implementation

Fork `thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `e7ba952` (PR https://github.com/thupalo/FlowEvo/pull/3): all three parts in one commit — `LLMResponse.finish_reason/reasoning`, `LLMClientError(status_code=, finish_reason=)`, `src/runtime/errors.py` (`classify_client_error`, `LLMGenerationError.from_client_error`, `RunAborted`), growth loop in `_generate_openai_chat` with the HTTP retry factored into `_post_with_retries`. 20 unit tests with a monkeypatched `requests.post`; verified live against a local Nemotron-3.5 server (`max_tokens=150`: default raises with `finish_reason="length"`, growth returns a complete, verifier-passing answer). Splitting into the three PRs above is straightforward from that commit.

## Related

- Hard-coded small budgets in ALFWorld (`alfworld_/generator.py:52` = 256, `alfworld_/compiler.py:233` = 500) make that runner unusable with reasoning models even after this fix — filed separately.
- Fork reference implementation and evidence: `thupalo/FlowEvo`, `FORK_GUIDE.md` Part C entries C-2026-08-25-a/b.
