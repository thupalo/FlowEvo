"""SQL generators: LLM-backed (via FlowEvo runtime) and an offline oracle.

Error handling contract of ``LLMSqlGenerator``:

* **Output token budget** — an empty reply (reasoning models exhaust
  ``max_tokens`` while thinking) or a reply that hit ``max_tokens`` without a
  closed SQL block is retried with the budget doubled, up to
  ``MAX_OUTPUT_TOKENS_CAP``.  If the cap is reached without a usable answer an
  ``LLMGenerationError(kind="output_budget_exhausted")`` is raised.
* **Input context length** — a 400/413 "context length" rejection makes the
  generator drop skill context (insight first, then exemplars) and retry.
  If the bare prompt is still too long, ``kind="context_length"`` is raised.
* **Transient failures** (rate limit, 5xx, transport) — the FlowEvo client
  already retries with back-off; if it still fails we surface
  ``kind in {rate_limit, server_error, transport}`` (``retryable=True``) and
  let the runner decide.
* **Fatal failures** (401/403, bad model name …) — ``fatal=True``; the runner
  aborts the run instead of failing 31 episodes one by one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .env import SqlEnvironment
from .errors import (
    CONTEXT_LENGTH,
    EMPTY_OUTPUT,
    OUTPUT_BUDGET_EXHAUSTED,
    OUTPUT_TRUNCATED,
    ConfigError,
    LLMGenerationError,
    classify_client_error,
)
from .schemas import SchemaExemplar, SchemaInsight, SqlTask

_SQL_BLOCK = re.compile(r"```(?:sql)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_OPEN_FENCE = re.compile(r"```(?:sql)?\s*\n", re.IGNORECASE)


@dataclass
class GenOutput:
    sql: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: str = ""
    max_output_tokens: int = 0     # budget in force for the successful call
    budget_retries: int = 0        # how many times the budget had to be grown
    context_shrinks: int = 0       # how many skill-context levels were dropped


_SQL_START = re.compile(r"\b(SELECT\b|WITH\s+(?:RECURSIVE\s+)?\w+\s+AS\s*\()", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Pull the SQL out of an LLM reply.

    Prefers a fenced ```sql block; otherwise takes the text from the first
    ``SELECT`` / CTE ``WITH x AS (`` onward.  Returns "" when the reply has
    neither, so the caller treats it as an empty answer rather than sending
    prose to the database.
    """
    m = _SQL_BLOCK.search(text)
    sql = m.group(1) if m else text
    sql = sql.strip().rstrip(";").strip()
    m2 = _SQL_START.search(sql)
    if m2:
        return sql[m2.start():]
    return sql if m else ""


def looks_truncated(text: str, completion_tokens: int, max_output_tokens: int) -> bool:
    """True when the reply used the whole budget and has no closed SQL block."""
    if max_output_tokens <= 0 or completion_tokens < max_output_tokens:
        return False
    if _SQL_BLOCK.search(text):
        return False
    # An opened-but-unclosed fence is the classic truncation shape; a reply
    # with no fence at all that still hit the limit is treated the same way.
    return True


class Generator(Protocol):
    def draft(self, task: SqlTask, *, schema_text: str, exemplars: list[SchemaExemplar], insight: SchemaInsight | None) -> GenOutput: ...
    def repair(self, task: SqlTask, *, schema_text: str, previous_sql: str, feedback: str, exemplars: list[SchemaExemplar], insight: SchemaInsight | None) -> GenOutput: ...


# ----------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------

SYSTEM = (
    "You are an expert SQL analyst working against a SQLite database. "
    "Write a single read-only SELECT query that answers the question exactly: "
    "return only the columns the question asks for, in the order asked, and nothing else. "
    "Use exact string equality for quoted values. Reply with the SQL inside a ```sql code block."
)


def _skill_block(exemplars: list[SchemaExemplar], insight: SchemaInsight | None) -> str:
    parts: list[str] = []
    if exemplars:
        parts.append("Previously solved questions on this database (reuse their join structure when relevant):")
        for e in exemplars:
            parts.append(f"Q: {e.question}\nTables: {', '.join(e.tables)}\nJoins: {'; '.join(e.join_path) or 'none'}\nSQL:\n```sql\n{e.sql}\n```")
    if insight is not None:
        rendered = insight.render()
        if rendered:
            parts.append("Database usage insights:\n" + rendered)
    return "\n\n".join(parts)


def build_draft_prompt(task: SqlTask, schema_text: str, exemplars: list[SchemaExemplar], insight: SchemaInsight | None) -> str:
    parts = [f"Database schema:\n{schema_text}"]
    skills = _skill_block(exemplars, insight)
    if skills:
        parts.append(skills)
    parts.append(f"Question: {task.question}\n\nWrite the SQL query.")
    return "\n\n".join(parts)


def build_repair_prompt(task: SqlTask, schema_text: str, previous_sql: str, feedback: str, exemplars: list[SchemaExemplar], insight: SchemaInsight | None) -> str:
    parts = [f"Database schema:\n{schema_text}"]
    skills = _skill_block(exemplars, insight)
    if skills:
        parts.append(skills)
    parts.append(
        f"Question: {task.question}\n\n"
        f"Your previous query:\n```sql\n{previous_sql}\n```\n\n"
        f"Verifier feedback: {feedback}\n\n"
        "Analyse what went wrong and write a corrected SQL query."
    )
    return "\n\n".join(parts)


# ----------------------------------------------------------------------
# LLM generator (uses FlowEvo runtime.llm_client)
# ----------------------------------------------------------------------

class LLMSqlGenerator:
    MAX_OUTPUT_TOKENS_CAP = 16384
    MAX_BUDGET_GROWTH_STEPS = 3  # 4096 -> 8192 -> 16384

    def __init__(
        self,
        config_path: str = "configs/default.yaml",
        *,
        model: str | None = None,
        max_output_tokens: int = 4096,
        client: Any = None,
    ) -> None:
        """Create a generator.

        ``client`` may be injected (tests) — it must expose
        ``generate(instructions=, input_text=, settings=)`` returning an
        object with ``text / prompt_tokens / completion_tokens / latency_ms``.
        """
        from runtime.config import GenerationSettings, RuntimeConfigError, load_runtime_config  # FlowEvo src
        from runtime.llm_client import LLMClient, LLMClientError

        self._settings_cls = GenerationSettings
        self._client_error_cls = LLMClientError
        if max_output_tokens <= 0:
            raise ConfigError("max_output_tokens must be positive")
        self.max_output_tokens = int(max_output_tokens)

        if client is not None:
            self.cfg = None
            self.client = client
        else:
            try:
                self.cfg = load_runtime_config(config_path=config_path, model=model)
            except (RuntimeConfigError, OSError, ValueError) as exc:
                raise ConfigError(f"Cannot load runtime config from {config_path}: {exc}") from exc
            try:
                self.client = LLMClient(self.cfg)
            except LLMClientError as exc:
                raise ConfigError(str(exc)) from exc

    # -- low level -------------------------------------------------------

    def _call_once(self, prompt: str, temperature: float, budget: int):
        settings = self._settings_cls(temperature=temperature, max_output_tokens=budget)
        return self.client.generate(instructions=SYSTEM, input_text=prompt, settings=settings)

    def _call(self, prompt: str, temperature: float) -> GenOutput:
        """One logical generation with output-budget growth.

        Raises ``LLMGenerationError``; ``kind == CONTEXT_LENGTH`` is meant to be
        caught by ``_generate_with_shrinking`` which drops prompt context.
        """
        budget = self.max_output_tokens
        retries = 0
        last_kind = EMPTY_OUTPUT
        while True:
            try:
                resp = self._call_once(prompt, temperature, budget)
            except self._client_error_cls as exc:
                kind, status = classify_client_error(exc)
                if kind != EMPTY_OUTPUT:
                    raise LLMGenerationError(kind=kind, message=str(exc), max_output_tokens=budget, http_status=status) from exc
                last_kind = EMPTY_OUTPUT
            else:
                sql = extract_sql(resp.text) if not looks_truncated(resp.text, resp.completion_tokens, budget) else ""
                if sql.strip():
                    return GenOutput(
                        sql=sql,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        latency_ms=resp.latency_ms,
                        raw=resp.text,
                        max_output_tokens=budget,
                        budget_retries=retries,
                    )
                # Either the reply hit the budget without a closed SQL block,
                # or it contained prose but no SQL at all — both are treated as
                # "no usable output" and get a bigger budget once more.
                last_kind = OUTPUT_TRUNCATED if looks_truncated(resp.text, resp.completion_tokens, budget) else EMPTY_OUTPUT

            # grow the budget or give up
            if retries >= self.MAX_BUDGET_GROWTH_STEPS or budget >= self.MAX_OUTPUT_TOKENS_CAP:
                raise LLMGenerationError(
                    kind=OUTPUT_BUDGET_EXHAUSTED,
                    message=f"no usable SQL after {retries} budget increases (last failure: {last_kind})",
                    max_output_tokens=budget,
                )
            budget = min(budget * 2, self.MAX_OUTPUT_TOKENS_CAP)
            retries += 1

    def _generate_with_shrinking(self, build, task: SqlTask, exemplars: list[SchemaExemplar], insight: SchemaInsight | None, temperature: float) -> GenOutput:
        """Call ``build(exemplars, insight)`` → prompt, shrinking context on
        context-length rejections: drop insight, then exemplars."""
        levels: list[tuple[list[SchemaExemplar], SchemaInsight | None]] = [(exemplars, insight)]
        if insight is not None:
            levels.append((exemplars, None))
        if exemplars:
            levels.append(([], None))
        for shrink, (ex, ins) in enumerate(levels):
            try:
                out = self._call(build(ex, ins), temperature)
            except LLMGenerationError as exc:
                if exc.kind == CONTEXT_LENGTH and shrink < len(levels) - 1:
                    continue
                raise
            out.context_shrinks = shrink
            return out
        raise AssertionError("unreachable")  # pragma: no cover

    # -- public API --------------------------------------------------------

    def draft(self, task, *, schema_text, exemplars, insight):
        return self._generate_with_shrinking(
            lambda ex, ins: build_draft_prompt(task, schema_text, ex, ins), task, exemplars, insight, temperature=0.0,
        )

    def repair(self, task, *, schema_text, previous_sql, feedback, exemplars, insight):
        return self._generate_with_shrinking(
            lambda ex, ins: build_repair_prompt(task, schema_text, previous_sql, feedback, ex, ins), task, exemplars, insight, temperature=0.3,
        )


# ----------------------------------------------------------------------
# Oracle generator (offline smoke tests; no API key needed)
# ----------------------------------------------------------------------

class OracleSqlGenerator:
    """Returns the gold SQL.  Optionally fails the first draft of selected
    task patterns to exercise the repair / escalation path.  Tokens are
    charged synthetically (proportional to prompt length) so that the
    'tokens saved by direct replay' signal is still visible in reports."""

    def __init__(self, env: SqlEnvironment, *, fail_first_patterns: set[str] | None = None) -> None:
        self.env = env
        self.fail_first_patterns = set(fail_first_patterns or ())
        self._failed_once: set[str] = set()

    def _charge(self, prompt: str, sql: str) -> tuple[int, int]:
        return max(1, len(prompt) // 4), max(1, len(sql) // 4)

    def draft(self, task, *, schema_text, exemplars, insight):
        prompt = build_draft_prompt(task, schema_text, exemplars, insight)
        if task.pattern in self.fail_first_patterns and task.task_id not in self._failed_once:
            self._failed_once.add(task.task_id)
            sql = "SELECT 1 FROM nonexistent_table"
        else:
            sql = task.gold_sql
        pt, ct = self._charge(prompt, sql)
        return GenOutput(sql=sql, prompt_tokens=pt, completion_tokens=ct, raw=sql)

    def repair(self, task, *, schema_text, previous_sql, feedback, exemplars, insight):
        prompt = build_repair_prompt(task, schema_text, previous_sql, feedback, exemplars, insight)
        pt, ct = self._charge(prompt, task.gold_sql)
        return GenOutput(sql=task.gold_sql, prompt_tokens=pt, completion_tokens=ct, raw=task.gold_sql)
