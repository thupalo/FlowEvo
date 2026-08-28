# Packaging: `alfworld` is not a declared dependency

## Summary

`pyproject.toml` declares four dependencies (`pydantic`, `datasets`, `PyYAML`, `requests`). `src/alfworld_/env.py:121` imports `alfworld.agents.environment` lazily when the environment is created, so on a fresh `pip install -e .` the ALFWorld runner starts, loads the config, and only then fails with `ModuleNotFoundError: No module named 'alfworld'`. The README's ALFWorld section does not mention the extra install or the game-file download (`alfworld-download`).

## Proposed change

`pyproject.toml` (current PyPI release is 0.4.2):

```toml
[project.optional-dependencies]
alfworld = ["alfworld>=0.4"]
dev = ["pytest>=8"]
```

README, under "Running ALFWorld":

```bash
pip install -e ".[alfworld]"
alfworld-download          # game files, once
```

## Reproducibility risk

None.

## Reference implementation

`thupalo/FlowEvo`, branch `core/upstream-backlog`, commit `ff77f64` (cherry-pickable as-is).
