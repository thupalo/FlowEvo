# Runners: fatal LLM errors are not distinguished from task failures; runs continue and pass rates silently drop

## Summary

Both experiment runners wrap each episode in a blanket `except Exception` and record the exception as a failed episode:

- `src/code_math/runner.py:604`
- `src/alfworld_/run_20task_validation.py:411`

Consequences observed:

1. A wrong API key or model name (HTTP 401/404) yields N individually failed episodes, each after the client's full retry/back-off cycle, instead of an immediate abort. On a 164-task HumanEval run that is many minutes of guaranteed failures.
2. A truncated or empty LLM reply (reasoning models, small `max_tokens`) is indistinguishable in the results from a wrong answer: it lowers `pass_rate` without any signal in the report.
3. No non-zero exit code, so CI or a batch script cannot detect the broken run.

## Proposed change

Depends on the typed-error classification from the reasoning-models issue (`runtime/errors.py`).

- Classify exceptions per episode: verifier failures and *retryable* LLM errors (rate limit, 5xx, transport) → record with a `failure_type` field and continue; *fatal* errors (auth, bad request) or `N` consecutive error episodes (default 3) → save the checkpoint, print the reason, raise / return non-zero.
- Add `failure_type` to the episode dict and a per-condition breakdown (`{execution_error: 3, output_truncated: 2, ...}`) to `generate_report`.
- Exit code: 0 ok, 1 aborted, 2 config error.

One schema note for ALFWorld: its error episodes currently store the raw exception message in `failure_type`. Proposal: `failure_type` becomes the category and the message moves to a new `error` key. Old checkpoints still load (the field is only read for reporting).

## Reproducibility risk

None: successful runs are unchanged; only the bookkeeping of failed/aborted runs differs.

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `02e8a45`: both runners record `failure_type` (`verification_failed`, `empty_output`, or the `runtime.errors` kind), abort via `RunAborted` on fatal kinds or `MAX_CONSECUTIVE_ERRORS = 3` after saving the checkpoint, `code_math` exits 1/2 and gains a "Failure types" report column, ALFWorld exits 1 and still writes the report for completed conditions. Tests in `tests/code_math/test_runner_policy.py` drive `run_condition` with a scripted LLM (fatal abort + checkpoint persisted, consecutive abort, isolated transport error recorded, prose reply → `empty_output`). Depends on the `runtime/errors.py` module from the reasoning-models issue.
