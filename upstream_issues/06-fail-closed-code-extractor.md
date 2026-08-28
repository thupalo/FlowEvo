# Code extraction returns prose when the reply has no code fence

## Summary

`extract_code` in `src/code_math/runner.py:240-255` looks for a ```` ```python ```` fence and, when none is found, returns the whole reply (`text.rstrip()`). A refusal, an explanation, or a reasoning-model reply that was cut off before the fence is therefore passed to the verifier as if it were a solution.

The same bug class appeared in a fork adapter: a fallback regex `\b(SELECT|WITH)\b` matched the English word *with* in "I cannot help with that." and executed `"with that."` as SQL.

## Why it matters

- The failure is recorded as a wrong answer, not as "model produced no code", so it is invisible in reports and indistinguishable from a real capability failure.
- In `ours` / `full_library` conditions the prose can be compiled into the skill library as a "solution" only if it passes tests — it won't — but it does count as a repair round and consumes retry budget.

## Proposed change

One shared helper in `src/core/utils.py`:

```python
def extract_fenced_code(text: str, *, lang: str = "python") -> str:
    """1. first fence tagged `lang` or untagged; 2. any other fence;
    3. the raw text only if it looks like code; else ''."""
```

"Looks like code" is a deliberately loose heuristic — a line starting with a Python keyword or decorator, or containing `=` / `(` — because HumanEval replies are often an unfenced, indented function *body* with no `def`, and a strict `^def ` start pattern would wrongly discard those. Prose such as "I cannot help with that." or a reasoning preamble cut off before the fence yields `""`.

`code_math.extract_code` and any future adapter use it and treat `""` as an *empty output* failure type (see the runners issue) rather than sending prose to the sandbox.

## Reproducibility risk

Low but non-zero: a prose reply currently fails the tests; after the change it fails as `empty_output` instead. Pass/fail outcome is the same; token accounting is the same; only the failure category differs. Worth stating in the PR.

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `848abc9`, with 8 tests (`tests/core/test_extract_fenced_code.py`) covering indentation preservation, tag preference, unfenced bodies, prose, and truncated-before-fence replies.
