# Runtime error handling for reasoning / self-hosted models, plus a self-contained SQL demo adapter

Hello, and thank you for releasing FlowEvo — the compile → replay → govern loop is a really useful idea and the codebase was pleasant to work with.

While setting the repository up against a **self-hosted reasoning model** (Nemotron-3.5 behind a vLLM-style OpenAI-compatible server, configured only through `configs/local.yaml`) I ran into a few robustness issues in the runtime and the experiment runners. I have fixed them in my fork and would be glad to contribute them back, in whatever granularity you prefer. I also built a small, offline-testable domain adapter that may be useful as a second example next to ALFWorld. Everything is on my fork's `main`: https://github.com/thupalo/FlowEvo — one commit per item so each can become a separate PR.

None of the changes alters the numbers of an unchanged experiment configuration; the one behaviour-changing feature (budget growth) is opt-in and off by default.

## 1. Empty replies from reasoning models (`src/runtime/llm_client.py`)

**Observed.** Reasoning models emit their thinking into `message.reasoning` before `content`. With the default budgets (`draft.max_output_tokens: 900`; ALFWorld 256 / 500 / 200) the budget is exhausted while thinking, the server returns `finish_reason: "length"` with `content: ""`, and the client raises `LLMClientError: OpenRouter returned empty content.` (`llm_client.py:167`). The `reasoning` field and `finish_reason` are discarded, so callers cannot tell a truncated reply from an empty answer. The same prompt succeeds with `max_tokens=4000` in ~650 completion tokens.

**Suggested correction** (fork commit `e7ba952`, additive):

- `LLMResponse` gains `finish_reason` and `reasoning`; `LLMClientError` gains `status_code` and `finish_reason` attributes, and the empty-content message reports `finish_reason=… reasoning_chars=…`.
- New `src/runtime/errors.py`: `classify_client_error()` → `empty_output`, `output_truncated`, `output_budget_exhausted`, `context_length`, `rate_limit`, `server_error`, `transport`, `auth`, `bad_request`, `malformed_response`; `LLMGenerationError` with `fatal` / `retryable` flags; `RunAborted` for experiment loops.
- Opt-in `llm.grow_on_truncation: true` (+ `llm.max_output_tokens_cap: 16384`): on `finish_reason == "length"` retry with `max_tokens` doubled, at most 3 steps. One detail worth flagging: the trigger has to be `finish_reason`, not "content is empty" — at an intermediate budget I saw *non-empty content cut off mid-answer*, which an emptiness check would have returned as complete.

Verified live: with the demo's full-schema prompt and `max_tokens=150`, the default raises with `finish_reason="length"`; with growth on, the call returns a complete, verifier-passing answer. 20 unit tests with a monkeypatched `requests.post`.

## 2. Hard-coded ALFWorld budgets (`src/alfworld_/generator.py:52`, `compiler.py:233`, `strategy_bank.py:132`)

These constants bypass the YAML config, so even after (1) ALFWorld cannot be used with a reasoning backbone without editing source. Suggested: an optional `llm.alfworld` block (`step_max_output_tokens`, `compile_max_output_tokens`, `strategy_max_output_tokens`) with the current values as defaults — existing configs behave identically. Fork commit `2e440f7`.

## 3. Runners treat every exception as a task failure (`code_math/runner.py:604`, `alfworld_/run_20task_validation.py:411`)

A wrong API key or model name produces N individually failed episodes (each after the client's full retry back-off) instead of an abort, and truncated replies are scored as wrong answers, silently lowering pass rates. Suggested: record a `failure_type` per episode, abort on fatal kinds (auth, bad request) or after 3 consecutive error episodes *after saving the checkpoint*, non-zero exit code, and a failure-type breakdown in the report. Successful runs are unchanged. Fork commit `02e8a45`, tests drive `run_condition` with a scripted LLM.

## 4. Two small correctness items

- **`Sandbox` runs candidate code under `python` from PATH** (`src/env/sandbox.py:14`), used with the default by `code_math/runner.py`, `compiler/admission.py` and `maintenance/governance.py`. In a venv on Windows/macOS this can be a different interpreter without the experiment's packages, and it shows up only as a lower pass rate. Suggested: default to `sys.executable`. Fork commit `c32ee79` (two lines).
- **`extract_code` returns prose when no fence is found** (`code_math/runner.py:240`), so a refusal or a reply truncated before the fence is executed as a solution. Suggested: a shared `core.utils.extract_fenced_code` that returns `""` when the reply contains no code (kept loose enough to accept unfenced, indented HumanEval bodies). Pass/fail outcome for such replies is the same; only the recorded category changes. Fork commit `848abc9`.

## 5. Small setup items

- `provider: openrouter` is really "any OpenAI-compatible endpoint"; accepting `openai_compatible` as an alias and sending the OpenRouter attribution headers / requiring the key only for `openrouter.ai` makes local servers a first-class path (`fc0a045`).
- `alfworld` is not a declared dependency (imported lazily at `alfworld_/env.py:121`); an `[project.optional-dependencies] alfworld` extra plus a README line for `alfworld-download` (`ff77f64`).
- README additions for local endpoints, reasoning-model budgets and a `--limit 5` smoke run (`f62f5b9`).

## 6. A second, self-contained domain adapter (proposal)

To check that I understood the framework, I applied the same loop to *natural-language questions over a SQLite database* ("get the address of customer X"): `democase_sql/` in the fork, following the `src/alfworld_/` layout (env with read-only execution + result-set verifier, three-layer library — parameterised SQL templates replayed with zero LLM tokens, schema exemplars, join/pitfall insights — compiler with replay-check admission, governance with contrastive holdout, an offline oracle generator, 30 pytest tests, no external dataset). On 31 questions with the local Nemotron backbone: pure dynamic 31/31 with 31 LLM calls / 39k tokens; full method 31/31 with 9 calls / 13.7k tokens (22 zero-token replays). If a small dependency-free example would be welcome in the repository (e.g. under `examples/`), I would be happy to adapt it to your conventions; a README link to the fork would be equally fine.

## How I'd suggest proceeding

If you agree in principle, I would open separate PRs in this order: (1) runtime fields + `errors.py` + opt-in growth, (4) the two small fixes, (2) ALFWorld budgets, (3) runner policy, (5) setup items — or a single PR if you prefer fewer reviews. Happy to adjust naming, split differently, or drop anything you consider out of scope. Thank you for your time!
