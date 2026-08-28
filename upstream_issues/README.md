# Upstream issue drafts

Ready-to-file issue texts for `DEFENSE-SEU/FlowEvo`, drafted from findings in
this fork (see `FORK_GUIDE.md` Part B for the backlog and Part C for evidence).
Each file is a complete GitHub issue body; the first line is the title.

| # | file | kind | scope | repro risk |
|---|---|---|---|---|
| 01 | [01-reasoning-models-typed-client-errors.md](01-reasoning-models-typed-client-errors.md) | issue → 3 PRs | `src/runtime/` | none (opt-in) |
| 02 | [02-openai-compatible-provider.md](02-openai-compatible-provider.md) | issue / small PR | `src/runtime/`, README | none |
| 03 | [03-alfworld-hardcoded-output-budgets.md](03-alfworld-hardcoded-output-budgets.md) | issue → PR | `src/alfworld_/` | none (defaults kept) |
| 04 | [04-runner-error-policy.md](04-runner-error-policy.md) | PR (needs 01) | `src/code_math/`, `src/alfworld_/` | none |
| 05 | [05-sandbox-uses-path-python.md](05-sandbox-uses-path-python.md) | one-line PR | `src/env/sandbox.py` | none |
| 06 | [06-fail-closed-code-extractor.md](06-fail-closed-code-extractor.md) | issue | `src/core/utils.py`, `src/code_math/` | low |
| 07 | [07-alfworld-optional-dependency.md](07-alfworld-optional-dependency.md) | PR | `pyproject.toml`, README | none |
| 08 | [08-readme-setup-gaps.md](08-readme-setup-gaps.md) | docs PR | README | none |
| 09 | [09-second-domain-adapter-example.md](09-second-domain-adapter-example.md) | discussion issue | new folder | none |

## Filing order

1. **01** as one issue, then its three PRs in sequence (response fields → errors module → opt-in growth).
2. **05** and **07** straight as PRs (no discussion needed).
3. **02, 03, 06, 09** as issues; **04** and **08** as PRs once 01 has landed.

## Commands

```powershell
# file an issue from a draft (title = first line, body = rest)
$f = "upstream_issues/01-reasoning-models-typed-client-errors.md"
$title = (Get-Content $f -TotalCount 1) -replace '^#\s*',''
$body  = (Get-Content $f | Select-Object -Skip 2) -join "`n"
gh issue create --repo DEFENSE-SEU/FlowEvo --title $title --body $body

# open a PR from a fork branch
gh pr create --repo DEFENSE-SEU/FlowEvo --base main --head thupalo:<branch> --title "..." --body-file <file>
```

Branch naming for PRs: `upstream/<nn>-<slug>` off `main` (which mirrors `upstream/main`).
