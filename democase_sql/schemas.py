"""Data structures for the SQL demo case."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SqlTask:
    """One natural-language question with a gold SQL query.

    ``pattern`` and ``params`` are ground-truth annotations used only for
    evaluation/reporting; the agent never sees them for routing.
    """

    task_id: str
    question: str
    gold_sql: str
    pattern: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SqlTask":
        return cls(**d)


@dataclass
class QueryTemplate:
    """Layer 1 — executable, parameterised SQL compiled from a success.

    ``question_signature`` is the question with literal values replaced by
    ``<p0>``, ``<p1>`` … in the same order as ``param_names``.  Routing of a
    new question to a template is done by matching signatures (no LLM).
    """

    template_id: str
    question_signature: str
    sql_template: str
    # Each entry: {"name": "p0_1", "lit": <index of quoted literal in question>,
    #              "part": <whitespace-part index within that literal, or -1 for whole>}
    param_spec: list[dict[str, Any]]
    tables: list[str]
    source_task_id: str
    utility: float = 1.0
    status: str = "active"  # active | suppressed
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_used_episode: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueryTemplate":
        return cls(**d)


@dataclass
class SchemaExemplar:
    """Layer 2 — a solved (question, sql) pair together with the schema
    fragment (tables, columns, join path) that made it work.  Injected as
    few-shot context into the generator prompt."""

    exemplar_id: str
    question: str
    sql: str
    tables: list[str]
    join_path: list[str]
    schema_fragment: str
    source_task_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SchemaExemplar":
        return cls(**d)


@dataclass
class SchemaInsight:
    """Layer 3 — cross-trace statistics about the database:
    which join paths are used most, which tables co-occur, which failure
    modes are common.  Injected as short directives."""

    join_frequency: dict[str, int] = field(default_factory=dict)
    table_frequency: dict[str, int] = field(default_factory=dict)
    common_pitfalls: list[str] = field(default_factory=list)
    sample_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SchemaInsight":
        return cls(**d)

    def render(self) -> str:
        lines: list[str] = []
        if self.join_frequency:
            top = sorted(self.join_frequency.items(), key=lambda kv: -kv[1])[:5]
            lines.append("Frequently used joins: " + "; ".join(f"{k} (x{v})" for k, v in top))
        if self.table_frequency:
            top = sorted(self.table_frequency.items(), key=lambda kv: -kv[1])[:5]
            lines.append("Frequently used tables: " + ", ".join(k for k, _ in top))
        for p in self.common_pitfalls[:3]:
            lines.append("Pitfall: " + p)
        return "\n".join(lines)


@dataclass
class SqlTrace:
    """Execution trace of one episode."""

    task_id: str
    question: str
    condition: str
    planning_mode: str  # direct_template | skill_seeded | pure_dynamic
    template_id: str = ""
    exemplar_ids: list[str] = field(default_factory=list)
    insight_used: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    final_sql: str = ""
    success: bool = False
    feedback: str = ""
    failure_type: str = ""  # "" on success; verifier category or LLM error kind otherwise
    budget_retries: int = 0  # output-budget growth retries across all calls
    context_shrinks: int = 0  # skill-context levels dropped due to context length
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    compiled_template_id: str = ""
    compiled_exemplar_id: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d
