# Sandbox: candidate code runs under `python` from PATH instead of the current interpreter

## Summary

`src/env/sandbox.py:14` defaults to `python_executable: str = "python"`, and three of the four call sites rely on that default:

- `src/code_math/runner.py:412` — `Sandbox(timeout_seconds=10.0)`
- `src/compiler/admission.py:109,167` — `Sandbox(timeout_seconds=...)` (skill admission gate)
- `src/maintenance/governance.py:47` — `Sandbox()` (skill audits)

Only `src/eval/runner.py:231` passes `python_executable` explicitly. Verification, skill admission and governance audits therefore execute candidate code with whatever `python` resolves to on `PATH`, not with the interpreter running the experiment.

## Why it matters

- Inside a virtualenv on Windows, `python` on PATH can be the system install or the `py` launcher's default, not `.venv\Scripts\python.exe`.
- On some Linux images `python` is Python 2 or absent (`python3` only) — every verification then fails with `FileNotFoundError` or syntax errors, which the runner records as task failures.
- Packages installed only in the venv (e.g. `numpy` needed by an MBPP test) are invisible to the sandbox.

All of these show up as a lower pass rate, not as an infrastructure error.

## Proposed change

```python
import sys
...
def __init__(self, python_executable: str | None = None, timeout_seconds: float = 5.0) -> None:
    self.python_executable = python_executable or sys.executable
```

Two lines; the explicit-argument path is unchanged.

## Reproducibility risk

None for environments where `python` already resolved to the experiment interpreter; otherwise it *fixes* a silent divergence.
