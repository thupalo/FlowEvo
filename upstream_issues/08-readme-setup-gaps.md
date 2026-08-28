# README: setup gaps found while onboarding

## Summary

A few things a new user hits in the first hour that the README could pre-empt. Docs-only PR.

1. **Run location.** README commands use `python -m src.code_math.runner ...` from the repo root; the runner docstrings (`src/code_math/runner.py:12-18`) say `cd src` and `python -m code_math.runner`. Both work; pick one and state it (the root form is what `pyproject.toml`'s package layout implies).
2. **Local / OpenAI-compatible endpoints.** State explicitly that `provider: openrouter` + a custom `base_url` works with vLLM / llama.cpp / LM Studio, with a 3-line `local.yaml` example.
3. **Reasoning models.** Note that models emitting hidden reasoning need a larger `max_output_tokens` than the defaults (900 in `configs/default.yaml`, 256/500 in ALFWorld) or the runtime raises "returned empty content".
4. **ALFWorld extras.** `pip install alfworld` and `alfworld-download` are required but unmentioned.
5. **`/docs/` is gitignored.** Root `.gitignore` ignores `/docs/`, `/results/`, `/analysis/` etc.; contributors adding documentation under `docs/` will silently lose it. Either un-ignore `docs/` or mention where docs should go.
6. **`--limit` for smoke runs.** The runner supports `--limit N`; a one-liner "quick check" example with `--limit 5` would save first-time users a full benchmark run.

## Reproducibility risk

None (docs only).

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`: items 2, 3, 5 and 6 in commit `f62f5b9` (README sections *Local / self-hosted models*, *Reasoning models*, *Quick check*, gitignore note, plus commented recipes in `configs/local.example.yaml`); item 4 in `ff77f64`; item 1 (runner docstrings vs README invocation) in a follow-up commit on the same branch.
