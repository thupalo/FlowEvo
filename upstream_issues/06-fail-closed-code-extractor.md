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
def extract_fenced_code(text: str, *, lang: str = "python", start_re: str = r"^\s*(def |class |import |from )") -> str:
    """Return the first fenced block; else text from the first line matching start_re; else ''."""
```

`code_math.extract_code` and any future adapter use it and treat `""` as an *empty output* failure type (see the runners issue) rather than sending prose to the sandbox.

## Reproducibility risk

Low but non-zero: a prose reply currently fails the tests; after the change it fails as `empty_output` instead. Pass/fail outcome is the same; token accounting is the same; only the failure category differs. Worth stating in the PR.
