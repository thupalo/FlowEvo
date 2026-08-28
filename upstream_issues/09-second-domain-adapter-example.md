# Proposal: a second, self-contained domain adapter example (NL → SQL over SQLite)

## Summary

The repository ships one domain adapter (ALFWorld) alongside the code/math benchmark runner. Both need external datasets or simulators, and the generic `src/agent` / `src/compiler` stack is specialised to Python-function tasks. A second adapter that is small, dependency-free and offline-testable would show how to apply the compile → replay → govern loop to a new domain, which is the paper's main claim.

I have such an adapter in my fork (`thupalo/FlowEvo`, folder `democase_sql/`, 16 files) and would like to know whether, and in what form, you would accept it. Its only dependency on core beyond `runtime.config` / `runtime.llm_client` is the `runtime/errors.py` module proposed in the reasoning-models issue; if that is not adopted, the adapter can carry its own copy (it originally did).

## What it is

- **Domain:** natural-language questions over a deterministic SQLite database (customers / addresses / orders / products); headline task "get the address of customer *X*".
- **Layout mirrors `src/alfworld_/`:** `env` (read-only execution, schema introspection, result-set verifier), `schemas`, `compiler`, three-layer `skill_library`, `generator`, `runner`, `tests`.
- **Layer 1** = parameterised SQL templates (`WHERE first_name = :p0_0 AND last_name = :p0_1`) admitted only if replay reproduces the original result; routed by an LLM-free question signature → zero-token replay.
- **Layer 2** = solved (question, SQL, schema fragment, join path) exemplars; **Layer 3** = join/table statistics + failure-mode pitfalls.
- **Governance:** per-template utility with suppression; contrastive holdout (every N-th seeded episode runs unguided) masking L2/L3 injection when it hurts.
- **Offline oracle generator** returning gold SQL (with deliberate first-draft failures) → the whole pipeline is covered by 30 pytest tests without an API key.
- **Runtime reuse:** only `runtime.config` / `runtime.llm_client`.

## Results (31 tasks, local Nemotron-3.5-30B backbone)

| condition | pass | LLM calls | tokens | zero-token replays |
|---|---|---|---|---|
| pure_dynamic | 31/31 | 31 | 39,059 | 0 |
| ours (L1+L2+L3, repair, governance) | 31/31 | 9 | 13,743 | 22 |

## Questions for maintainers

1. Would you accept it, and where: `src/sql_demo/` (parallel to `alfworld_`), `examples/sql/`, or a link from the README to the fork?
2. Should it reuse `core.schemas.SkillCard` / `ExecutionTrace` (it currently has lighter dataclasses with the same field names for `planning_mode`, attempts and token accounting) — I can adapt if you prefer a single schema.
3. Any preference on the task set format (currently a Python module generating questions with double-quoted literals so the parameter extractor is deterministic)?

## Reproducibility risk

None — additive folder; the benchmark runners are untouched.
