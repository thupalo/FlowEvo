# FlowEvo fork guide

Living document for this fork (`thupalo/FlowEvo`, upstream `DEFENSE-SEU/FlowEvo`).
Two audiences:

1. **Application builders** — how to put FlowEvo's compile-and-reuse loop on a
   new domain (Part A).
2. **Fork maintainers** — what the core needs, tracked as a PR backlog with
   status and reproducibility risk (Part B).

Part C is an append-only findings log. Add a dated entry whenever a demo,
experiment or code review surfaces something; promote it into A or B once it
is confirmed.

Reference application: [`democase_sql/`](democase_sql/) (NL question → SQL over
SQLite; paper idea applied to "get the address of customer X").

---

## Part A — Building an application on FlowEvo

### A.1 Which code to build on

| Layer | Status for a new domain |
|---|---|
| `src/runtime/` (`config.py`, `llm_client.py`) | **Reuse as-is.** Any OpenAI-compatible endpoint via `configs/local.yaml`. |
| `src/agent/`, `src/compiler/`, `src/memory/`, `src/governance/`, `src/maintenance/` | **Not reusable outside Python code benchmarks.** Hard-wired to `CodeTaskInstance`, entry-point matching, assert harnesses. Read for design ideas, do not import. |
| `src/alfworld_/` | **The template.** The framework's own example of a domain adapter. Copy its file layout. |
| `src/code_math/runner.py` | Simplest end-to-end loop (`CodeSkillLibrary` + LLM + verify + retry + checkpoint). Good first read. |

### A.2 Adapter layout (copy this)

```
myapp/
  __init__.py        # puts ../src on sys.path
  env.py             # the world: execute, introspect, verify (deterministic!)
  schemas.py         # Task, Template (L1), Exemplar (L2), Insight (L3), Trace
  tasks.py           # task stream with repeated patterns
  compiler.py        # success trace -> L1 template (+ replay check) + L2 exemplar
  skill_library.py   # 3 layers + retrieval + governance + JSON persistence
  generator.py       # prompt assembly; LLM generator + offline oracle
  runner.py          # conditions, episode loop, report
  tests/             # offline tests via the oracle generator
  .gitignore         # runs/, generated data
```

### A.3 Checklist of things that mattered

- **Deterministic verifier first.** Everything (compile admission, governance,
  contrastive eval) assumes a cheap, trustworthy pass/fail. For SQL: execute
  candidate and gold, compare result multisets. No LLM-as-judge.
- **Read-only environment.** Open the DB with `mode=ro`, reject non-SELECT
  statements, treat `sqlite3.Warning` (multi-statement) as an error.
- **Layer 1 must be executable with zero LLM tokens.** Parameterise the
  successful artifact (literals → `:params`) and admit it only if replaying
  with the original values reproduces the original result.
- **Route L1 without an LLM.** Normalise the question into a *signature*
  (literals → `<p0>`), match exact or Jaccard ≥ 0.85, bind literals. This is
  what makes replay cheaper than generation.
- **Multi-word literals** (`"Alice Smith"`) may appear split in the artifact
  (`first_name='Alice' AND last_name='Smith'`); the compiler must handle both.
- **Task stream must repeat patterns** or L1 never fires and the experiment
  shows nothing.
- **Offline oracle generator** returning gold answers (with a deliberate
  first-draft failure on some patterns) lets the whole pipeline — compile,
  replay, repair, governance — be tested without an API key. Charge synthetic
  tokens (≈ chars/4) so "tokens saved by replay" still shows in reports.
- **Governance from day one:** per-template `use/success/failure` with
  suppression; contrastive holdout (every Nth seeded episode runs unguided)
  masking L2/L3 injection when it hurts. L1 is never masked — it does not go
  through the LLM.
- **Reasoning models** (Nemotron, DeepSeek-R1, Qwen-thinking) spend output
  tokens on hidden `reasoning` before `content`. Small `max_tokens` → empty
  `content` → `LLMClientError: returned empty content`. Default to ≥ 4096 and
  grow on demand. See B-1/B-2.
- **Typed error policy** in the episode loop: verifier failure and retryable
  LLM errors → record, continue; fatal (401/403, unknown model) or N
  consecutive errors → abort, *after* persisting partial results; config
  errors → exit 2 with a hint. Record `failure_type` on every trace so
  token-limit trouble is visible instead of silently lowering pass rates.
- **Extractors must fail closed.** `\bWITH\b` matched the English word in
  "I cannot help with that." and sent `"with that."` to the database. Require
  a real SQL start (`SELECT` / `WITH x AS (`) and return `""` otherwise.
- **Git hygiene:** `runs/`, generated DBs and `configs/local.yaml` are
  ignored; upstream's root `.gitignore` also ignores `/docs/`, so committed
  documentation lives at the root or inside the app folder.

### A.4 Results to expect (SQL demo, 31 tasks, local Nemotron-3.5-30B)

| condition | pass | LLM calls | tokens | L1 replays |
|---|---|---|---|---|
| pure_dynamic | 31/31 | 31 | 39,059 | 0 |
| ours | 31/31 | 9 | 13,743 | 22 |

---

## Part B — Core fork PR backlog

Status: `idea` → `planned` → `branch` → `merged` → `upstreamed`.
Repro risk = does it change the numbers of an unchanged experiment run?

| ID | Title | Files | Status | Repro risk | Origin |
|---|---|---|---|---|---|
| B-1 | Expose `finish_reason` and `reasoning` on `LLMResponse`; add `status_code` to `LLMClientError` | `src/runtime/llm_client.py` | branch `core/upstream-backlog` (e7ba952) | none (additive) | C-2026-08-25-a |
| B-2 | Opt-in output-budget growth in `LLMClient.generate` (`finish_reason == "length"` with empty content → double `max_tokens` up to a cap) | `src/runtime/llm_client.py`, `src/runtime/config.py` | branch (e7ba952), `grow_on_truncation` default off | **yes if default-on** — shipped default-off | C-2026-08-25-a |
| B-3 | `runtime/errors.py`: classify client failures (`empty_output`, `output_truncated`, `context_length`, `rate_limit`, `server_error`, `transport`, `auth`, `bad_request`, `malformed_response`) with `fatal`/`retryable` flags | `src/runtime/errors.py`; `democase_sql/errors.py` re-exports it | branch (e7ba952, f46f24b) | none | C-2026-08-25-b |
| B-4 | Experiment runners: abort on fatal error / N consecutive errors, persist partial results, exit codes, `failure_type` per episode | `src/code_math/runner.py`, `src/alfworld_/run_20task_validation.py` | branch (02e8a45) | none | C-2026-08-25-b |
| B-5 | ALFWorld / compiler output budgets (256, 500, 200) config-driven via `llm.alfworld` | `src/runtime/config.py`, `src/alfworld_/{generator,compiler,strategy_bank}.py` | branch (2e440f7), defaults unchanged | none with defaults | C-2026-08-25-a |
| B-6 | Document local / reasoning-model setup in `configs/local.example.yaml` and README | `configs/local.example.yaml`, `README.md` | branch (f62f5b9) | none | C-2026-08-25-a |
| B-7 | Shared fail-closed fenced-block extractor used by `code_math.extract_code` | `src/core/utils.py`, `src/code_math/runner.py` | branch (848abc9) | low (failure category only) | C-2026-08-25-c |
| B-8 | `alfworld` as an optional extra in `pyproject.toml` | `pyproject.toml`, `README.md` | branch (ff77f64) | none | C-2026-08-24 |
| B-9 | `openai_compatible` provider alias; OpenRouter headers / api_key only for openrouter.ai | `src/runtime/config.py`, `src/runtime/llm_client.py` | branch (fc0a045) | none | C-2026-08-28 |
| B-10 | `Sandbox` defaults to `sys.executable` instead of `python` on PATH | `src/env/sandbox.py` | branch (c32ee79) | none (fixes silent divergence) | C-2026-08-28 |

Suggested order: B-1 → B-3 → B-2 → B-6, then B-4/B-5/B-7 as separate PRs.
Everything with repro risk stays opt-in so the fork remains mergeable upstream.

Ready-to-file upstream issue texts for these items live in
[`upstream_issues/`](upstream_issues/README.md) (mapping: 01 = B-1/B-2/B-3,
02 = provider naming, 03 = B-5, 04 = B-4, 05 = sandbox interpreter,
06 = B-7, 07 = B-8, 08 = B-6, 09 = contributing `democase_sql/`).
Update the backlog status column when an issue or PR is opened.

---

## Part D — Fork sync workflow

Remotes: `origin` = `thupalo/FlowEvo` (this fork), `upstream` =
`DEFENSE-SEU/FlowEvo` (paper code).

Rules:

- `main` is a **clean mirror of `upstream/main`** — never commit to it
  directly. That keeps upstream PRs trivial and lets `git diff upstream/main`
  show exactly what the fork adds.
- All fork work lives on branches (`feature/<name>`, `core/B-<n>` for backlog
  items) and enters `main` through PRs on the fork.
- Backlog items with reproducibility risk (Part B) stay opt-in so the fork
  can always fast-forward to upstream.

Sync `main` with upstream (run whenever upstream moves):

```powershell
git fetch upstream
git switch main
git merge --ff-only upstream/main      # fails loudly if main drifted
git push origin main
```

Rebase a working branch onto the refreshed main:

```powershell
git switch feature/democase-sql
git rebase main
git push --force-with-lease
```

Check state at any time:

```powershell
git fetch --all
git log --oneline --graph --decorate main upstream/main origin/main -5
```

---

## Part C — Findings log (append-only)

### C-2026-08-24 — environment setup
- Repo cloned, `.venv` (Python 3.12) with editable install works; `alfworld`
  is not a declared dependency (→ B-8).
- Upstream remote `DEFENSE-SEU/FlowEvo`; fork identical at `36e81ef`.

### C-2026-08-25-a — reasoning model behind `local.yaml` breaks the runtime
- Model: `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (vLLM-style
  server). Response carries thinking in `message.reasoning`; with
  `max_tokens=600` → `finish_reason: length`, `content: ""`.
- `src/runtime/llm_client.py:167` raises on empty content and discards the
  `reasoning` field; no caller can tell truncation from an empty answer.
- Same prompt with `max_tokens=4000` succeeds in 654 completion tokens.
- Fix applied in the demo (`democase_sql/generator.py`): default 4096 with
  growth ×2 up to 16384. Verified: `--max-output-tokens 600` now shows
  `budget+1`/`budget+2` and passes. → B-1, B-2, B-5, B-6.

### C-2026-08-25-b — error handling review
- Core uses blanket `except Exception` in runners
  (`code_math/runner.py:604`, `alfworld_/run_20task_validation.py:411`): a
  bad API key fails every episode individually; context-length 400 and
  unknown-model 400 are indistinguishable.
- Demo now has `errors.py` with message-based classification of
  `LLMClientError` (formats: `HTTP <code> for <url>. Body: …`,
  `ConnectionError: …`, `Invalid JSON: …`, `Exceeded retry limit …`,
  `returned empty content`). 21 tests with a scripted fake client. → B-3, B-4.

### C-2026-08-25-c — extractor bug
- `extract_sql` fallback `\bWITH\b` matched prose; returned `"with that."`
  as SQL. Fixed to require `SELECT` or `WITH x AS (` and to return `""`
  otherwise. `code_math.extract_code` has the same fence regex family. → B-7.

### C-2026-08-28 — core backlog implemented on `core/upstream-backlog`
- Nine commits, one per `upstream_issues/` item, ordered so each can be
  cherry-picked into its own upstream PR. 70 tests (`tests/` + demo) pass.
- While implementing: the `Sandbox()` default is used by `compiler/admission.py`
  and `maintenance/governance.py` too, not only the code/math runner — so
  skill admission and audits also ran under PATH `python`.
- `alfworld` is imported lazily (`alfworld_/env.py:121`), so the missing
  dependency surfaces at environment creation, not at import.
- Verified against the local Nemotron server with the demo's full-schema
  prompt and `max_tokens=150`: default config raises `LLMClientError`
  (`finish_reason="length"`, classified `output_truncated`); with
  `grow_on_truncation: true` the call succeeds after budget growth.
- Found while verifying: at an intermediate budget the reply was *non-empty
  but cut off mid-SQL* with `finish_reason="length"`. Growth must therefore
  trigger on `finish_reason == "length"` regardless of content, not only on
  empty content — fixed in the runtime; a short prompt (answer < budget)
  does not reproduce the problem, so verify with a hard prompt.

### C-2026-08-25-d — adapter design decisions that held up
- Literal → parameter compile with replay check admitted 9 templates in 31
  episodes with zero false replays (all 22 L1 episodes correct, both with the
  oracle and with the real model).
- Contrastive holdout never triggered suppression on this task set (model
  too strong); needs a harder task set to be exercised.
