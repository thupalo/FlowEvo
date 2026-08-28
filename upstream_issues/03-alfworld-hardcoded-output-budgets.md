# ALFWorld: output-token budgets are hard-coded (256 / 500) and bypass the runtime config

## Summary

The ALFWorld adapter ignores the `draft` / `repair` generation settings from `configs/*.yaml` and uses module-level constants:

- `src/alfworld_/generator.py:52` — `_STEP_SETTINGS = GenerationSettings(temperature=0.0, max_output_tokens=256)`
- `src/alfworld_/compiler.py:233` — `GenerationSettings(temperature=0.0, max_output_tokens=500)`
- `src/alfworld_/strategy_bank.py:132` — `max_output_tokens=200`

The comment at `generator.py:50-51` explains why 256 was chosen for the paper's backbone ("Think/Act format needs room for reasoning … previous 64 prevented any reasoning"). With a reasoning backbone (hidden `reasoning` field, see the reasoning-models issue) these budgets are exhausted before any `content` is produced, so every step raises `LLMClientError: returned empty content` and the run cannot start. There is no way to change this without editing source.

## Steps to reproduce

1. Configure a reasoning model in `configs/local.yaml`.
2. `python -m src.alfworld_.run_20task_validation --config-path configs/default.yaml --output-dir runs/x --conditions pure_dynamic`

## Proposed change

Read budgets from `RuntimeLLMConfig` with the current constants as defaults, e.g. an optional `llm.alfworld` block:

```yaml
llm:
  alfworld:
    step_max_output_tokens: 256
    compile_max_output_tokens: 500
    strategy_max_output_tokens: 200
```

`load_runtime_config` already merges nested dicts, so this is ~10 lines in `config.py` plus replacing the three constants with attribute reads.

## Reproducibility risk

None when the defaults are unchanged; the paper configuration remains the default.

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `2e440f7`: `AlfWorldGenerationBudgets` dataclass on `RuntimeLLMConfig.alfworld`, `alfworld_budgets(llm_client)` helper that falls back to the defaults for stub clients (tests), and the three call sites reading through it. Existing YAML files need no change.

## Related

- Reasoning-model support in the runtime (separate issue) — necessary but not sufficient for ALFWorld because of these constants.
