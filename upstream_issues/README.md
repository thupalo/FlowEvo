# Upstream issue drafts

Ready-to-file issue texts for `DEFENSE-SEU/FlowEvo`, drafted from findings in
this fork (see `FORK_GUIDE.md` Part B for the backlog and Part C for evidence).
Each file is a complete GitHub issue body; the first line is the title.

All code items are implemented on the fork branch `core/upstream-backlog`
(https://github.com/thupalo/FlowEvo/pull/3), one commit per item, so each
upstream PR is a cherry-pick. Every draft ends with a *Reference
implementation* section naming its commit.

| # | file | kind | scope | repro risk | fork commit |
|---|---|---|---|---|---|
| 00 | [00-umbrella-issue.md](00-umbrella-issue.md) | single umbrella issue (paste-ready) | all of the below | — | `main` |
| 01 | [01-reasoning-models-typed-client-errors.md](01-reasoning-models-typed-client-errors.md) | issue → 3 PRs | `src/runtime/` | none (opt-in) | `e7ba952` |
| 02 | [02-openai-compatible-provider.md](02-openai-compatible-provider.md) | issue / small PR | `src/runtime/`, README | none | `fc0a045` |
| 03 | [03-alfworld-hardcoded-output-budgets.md](03-alfworld-hardcoded-output-budgets.md) | issue → PR | `src/runtime/config.py`, `src/alfworld_/` | none (defaults kept) | `2e440f7` |
| 04 | [04-runner-error-policy.md](04-runner-error-policy.md) | PR (needs 01) | `src/code_math/`, `src/alfworld_/` | none | `02e8a45` |
| 05 | [05-sandbox-uses-path-python.md](05-sandbox-uses-path-python.md) | one-line PR | `src/env/sandbox.py` | none | `c32ee79` |
| 06 | [06-fail-closed-code-extractor.md](06-fail-closed-code-extractor.md) | issue | `src/core/utils.py`, `src/code_math/` | low | `848abc9` |
| 07 | [07-alfworld-optional-dependency.md](07-alfworld-optional-dependency.md) | PR | `pyproject.toml`, README | none | `ff77f64` |
| 08 | [08-readme-setup-gaps.md](08-readme-setup-gaps.md) | docs PR | README, `configs/local.example.yaml` | none | `f62f5b9` |
| 09 | [09-second-domain-adapter-example.md](09-second-domain-adapter-example.md) | discussion issue | new folder (depends on 01's `runtime/errors.py`) | none | PR #1 |

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

Branch naming for PRs: `upstream/<nn>-<slug>` off `main` (which mirrors `upstream/main`), e.g.

```powershell
git switch -c upstream/05-sandbox-sys-executable upstream/main
git cherry-pick c32ee79
git push -u origin upstream/05-sandbox-sys-executable
gh pr create --repo DEFENSE-SEU/FlowEvo --base main --head thupalo:upstream/05-sandbox-sys-executable --title "..." --body-file upstream_issues/05-sandbox-uses-path-python.md
```

Items 02, 03 and 04 touch `src/runtime/config.py` / `runtime/errors.py` and
should be filed after 01 lands (or rebased onto the 01 branch).
