# FlowEvo demo case: self-evolving SQL agent

A small, self-contained domain adapter that applies the FlowEvo idea —
*compile successful execution traces into reusable, directly executable
skills, then route future tasks to the cheapest reliable path* — to
**answering natural-language questions against a SQL database**.

Headline example: *"Get the address of the customer "Alice Smith""*.
The first time, the agent inspects the schema and asks the LLM for SQL.
The verifier confirms the result, the compiler lifts the literal values into
parameters, and the query becomes a **Layer‑1 template**. Every later
"address of customer X" question is answered by **direct replay with zero
LLM tokens**.

## Why this layout

The generic `src/agent` + `src/compiler` stack in FlowEvo is hard-wired to
Python code benchmarks (entry points, assert harnesses). The framework's
intended way to add a new domain is the **ALFWorld adapter pattern**
(`src/alfworld_/`): domain `env`, `schemas`, three-layer `skill_library`,
`compiler`, `generator`, and a `runner` with experimental conditions +
governance. This folder follows exactly that structure, and reuses the
shared FlowEvo runtime (`src/runtime/config.py`, `src/runtime/llm_client.py`)
for LLM access.

| File | Role | FlowEvo counterpart |
|---|---|---|
| `db/build_db.py` | deterministic SQLite DB (countries, addresses, customers, categories, products, orders, order_items) | benchmark data |
| `env.py` | read-only execution, schema introspection ("data structures"), result-set verifier | `env/sandbox.py`, `eval/verifier.py` |
| `tasks.py` | 31 NL questions with gold SQL, repeated patterns | `code_math/loader.py` |
| `schemas.py` | `SqlTask`, `QueryTemplate`, `SchemaExemplar`, `SchemaInsight`, `SqlTrace` | `core/schemas.py`, `alfworld_/schemas.py` |
| `compiler.py` | trace → parameterised template + schema exemplar; replay check | `compiler/`, `alfworld_/compiler.py` |
| `skill_library.py` | 3 layers + retrieval + governance (utility, suppression, contrastive eval) | `memory/`, `governance/`, `alfworld_/skill_library.py` |
| `generator.py` | prompt assembly; `LLMSqlGenerator` (FlowEvo runtime) and `OracleSqlGenerator` (offline) | `agent/generator.py` |
| `runner.py` | conditions, episode loop, JSON traces + Markdown report | `alfworld_/run_20task_validation.py` |
| `tests/test_smoke.py` | offline tests, no API key needed | — |

## The three skill layers

1. **QueryTemplate (executable)** — SQL with named parameters
   (`WHERE c.first_name = :p0_0 AND c.last_name = :p0_1`) and a
   *question signature* (`get the address ... of the customer <p0>`).
   Routing is done without an LLM: a new question is normalised into a
   signature, matched against templates (exact or Jaccard ≥ 0.85), and its
   quoted literals are bound to the parameters. Multi-word literals like
   `"Alice Smith"` are split when the SQL used the parts separately.
   A template is only admitted if replaying it with the original literals
   reproduces the original result.
2. **SchemaExemplar (workflow knowledge)** — the solved `(question, SQL)`
   pair together with the tables, FK join path and schema fragment it used.
   The two most similar exemplars are injected as few-shot context when no
   template matches ("skill-seeded" generation).
3. **SchemaInsight (statistics)** — join/table frequencies across successes
   and failure-mode percentages (execution error, wrong columns, wrong
   rows, wrong values) rendered as short directives.

Governance:

* each template tracks `use/success/failure`; two failures with utility < 0.5
  → `suppressed` (a fresh success can then replace it);
* **contrastive evaluation**: every 5th seeded episode of a signature
  cluster runs *unguided*; if guided success rate is materially worse than
  unguided, exemplar/insight injection is masked for that cluster
  (Layer 1 is never masked — it does not go through the LLM).

## Conditions

| condition | compile | L1 replay | L2/L3 seeding | repair rounds | governance |
|---|---|---|---|---|---|
| `pure_dynamic` | – | – | – | 0 | – |
| `compile_only` | ✓ | – | – | 0 | – |
| `layer1_only` | ✓ | ✓ | – | 0 | ✓ |
| `full_library` | ✓ | ✓ | ✓ | 0 | ✓ |
| `ours` | ✓ | ✓ | ✓ | 2 | ✓ |
| `no_governance` | ✓ | ✓ | ✓ | 2 | – |

## Quick start

```powershell
.venv\Scripts\Activate.ps1

# 1. build the demo database (idempotent, deterministic)
python -m democase_sql.db.build_db

# 2. offline run with the oracle generator (no API key)
python -m democase_sql.runner --generator oracle `
    --output-dir democase_sql/runs/oracle_demo `
    --conditions pure_dynamic layer1_only full_library ours

# 3. real run with an LLM through the FlowEvo runtime
#    (needs api_key in configs/local.yaml or OPENROUTER_API_KEY)
python -m democase_sql.runner --generator llm --config-path configs/default.yaml `
    --output-dir democase_sql/runs/llm_demo --conditions pure_dynamic ours

# tests
python -m pytest democase_sql/tests -q
```

### Local / self-hosted models

Any OpenAI-compatible server works (vLLM, llama.cpp, LM Studio, Ollama …):
set `base_url`, `model` and `api_key` in `configs/local.yaml` (the runtime
still calls the provider `openrouter`, it is just the chat-completions
protocol). **Reasoning models** (Nemotron 3.5, DeepSeek-R1, Qwen-thinking …)
emit hidden thinking into a `reasoning` field before the answer; if
`max_tokens` is too small the thinking exhausts the budget and the server
returns an empty `content`, which surfaces as
`LLMClientError: OpenRouter returned empty content`. The runner therefore
uses `--max-output-tokens 4096` by default and grows the budget on demand
(see below).

### Error handling

FlowEvo's `LLMClient` folds every failure into one `LLMClientError` whose
message carries the detail. [errors.py](errors.py) classifies those messages
and the demo reacts per category:

| situation | kind | reaction |
|---|---|---|
| empty `content` (reasoning ate `max_tokens`) | `empty_output` | double the budget, retry |
| reply hit `max_tokens` with no closed ```` ```sql ```` block | `output_truncated` | double the budget, retry |
| still nothing at the cap (4096 → 8192 → 16384) | `output_budget_exhausted` | episode fails, run continues |
| 400/413 "context length" | `context_length` | drop insight, then exemplars, retry; bare prompt too long → episode fails |
| 429 / 5xx / connection errors (after the client's own back-off) | `rate_limit` / `server_error` / `transport` | episode fails, run continues |
| 401 / 403, unknown model, other 4xx | `auth` / `bad_request` | **run aborts** (partial results saved) |
| N consecutive error episodes (default 3) | — | **run aborts** |
| `configs/*.yaml` missing key / model | `ConfigError` | exit code 2 with a hint |

Every trace records `failure_type`, `budget_retries` and `context_shrinks`,
and the report has `budget↑ / shrink / errors / failure types` columns so
token-limit trouble is visible instead of silently lowering pass rates.
Verifier failures are categorised too (`execution_error`, `wrong_columns`,
`wrong_rows`, `wrong_values`), and the same categories feed the Layer-3
pitfall insights.

Outputs per run: `tasks.json`, `<condition>/episodes.json` (full traces:
attempts, SQL, feedback, tokens, planning mode), `<condition>/skill_library.json`
(the compiled templates / exemplars / insight — the gathered experience),
and `report.md` (per-condition table).

The oracle generator returns gold SQL but deliberately fails the first draft
of two patterns (`customer_total_spent`, `top_customers_by_spend`) so the
repair path and the difference between `full_library` and `ours` are visible
offline. Tokens are charged synthetically (≈ chars / 4) so the "tokens saved
by replay" signal is still meaningful.

## Reading the results

Look at three things in `report.md` / the console log:

* **mode column** — `L1` episodes cost 0 LLM calls; watch how quickly the
  stream converts from `dyn` → `L2/3` → `L1` as patterns recur.
* **tokens** — `ours` vs. `pure_dynamic` total tokens.
* **`skill_library.json`** — the stored data structures and queries: each
  template's `sql_template`, `param_spec`, `tables`, utility counters; each
  exemplar's `join_path` and `schema_fragment`.

## Extending

* **Your own database** — point `--db` at any SQLite file and replace
  `tasks.py` with your questions (quote literal values with double quotes so
  the parameter extractor can bind them). Postgres/MySQL: implement the same
  four methods of `SqlEnvironment` (`schema`, `execute`, `compare`,
  `tables_in_sql`) on top of a read-only connection.
* **Parameter extraction** — currently deterministic (double-quoted values
  in order). For free-form questions, replace `extract_literals` with a
  small LLM call that returns the literal list; everything downstream stays.
* **Richer routing** — swap the Jaccard signature match for embeddings.
* **Porting back to FlowEvo core** — `QueryTemplate` ≈ `SkillCard` with
  `callable_type="sql_template"`; `SqlTrace` carries the same
  `planning_mode` / attempt / token fields as `core.schemas.ExecutionTrace`.
