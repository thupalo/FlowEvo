"""Compile successful SQL traces into reusable skills.

* Layer 1: parameterised ``QueryTemplate`` — literal values that appear both
  in the question (double-quoted) and in the SQL (single-quoted) are lifted
  into named parameters.  The question with literals replaced becomes the
  routing signature.
* Layer 2: ``SchemaExemplar`` — the solved pair plus the schema fragment
  (tables / FK join path) that the query touched.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from .env import SqlEnvironment
from .schemas import QueryTemplate, SchemaExemplar

_QUOTED = re.compile(r'"([^"]+)"')
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w<>\s]")


# ----------------------------------------------------------------------
# Question analysis (shared by compiler and router)
# ----------------------------------------------------------------------

def extract_literals(question: str) -> list[str]:
    """Double-quoted values in the question, in order of appearance."""
    return [m.group(1).strip() for m in _QUOTED.finditer(question)]


def question_signature(question: str) -> str:
    """Question with each quoted literal replaced by <pN>, normalised."""
    idx = 0

    def repl(_m: re.Match[str]) -> str:
        nonlocal idx
        s = f"<p{idx}>"
        idx += 1
        return s

    sig = _QUOTED.sub(repl, question)
    sig = _PUNCT.sub(" ", sig.lower())
    return _WS.sub(" ", sig).strip()


def signature_tokens(sig: str) -> set[str]:
    return {t for t in sig.split() if not t.startswith("<p")}


def bind_params(spec: list[dict[str, Any]], literals: list[str]) -> dict[str, Any] | None:
    """Bind question literals to a template's parameter spec.  Returns None
    when the question does not carry enough literals / parts."""
    out: dict[str, Any] = {}
    for p in spec:
        li, part = int(p["lit"]), int(p["part"])
        if li >= len(literals):
            return None
        value = literals[li]
        if part >= 0:
            parts = value.split()
            if part >= len(parts):
                return None
            value = parts[part]
        out[str(p["name"])] = value
    return out


# ----------------------------------------------------------------------
# Compiler
# ----------------------------------------------------------------------

@dataclass
class CompileResult:
    template: QueryTemplate | None
    exemplar: SchemaExemplar | None
    reason: str = ""


class SqlCompiler:
    def __init__(self, env: SqlEnvironment) -> None:
        self.env = env

    # -- Layer 1 --------------------------------------------------------

    def _parameterise(self, sql: str, literals: list[str]) -> tuple[str, list[dict[str, Any]]] | None:
        """Replace single-quoted occurrences of question literals with :params.

        Tries the whole literal first, then whitespace-separated parts
        (e.g. "Alice Smith" -> first_name='Alice' AND last_name='Smith').
        Returns None when some literal cannot be located in the SQL, which
        means the query does not generalise by simple substitution.
        """
        spec: list[dict[str, Any]] = []
        out = sql
        for li, lit in enumerate(literals):
            whole = f"'{lit}'"
            if whole in out:
                name = f"p{li}"
                out = out.replace(whole, f":{name}")
                spec.append({"name": name, "lit": li, "part": -1})
                continue
            parts = lit.split()
            if len(parts) < 2:
                return None
            found_any = False
            for pi, part in enumerate(parts):
                q = f"'{part}'"
                if q in out:
                    name = f"p{li}_{pi}"
                    out = out.replace(q, f":{name}")
                    spec.append({"name": name, "lit": li, "part": pi})
                    found_any = True
            if not found_any:
                return None
        # Any remaining string literal means the query is still bound to a
        # value not present in the question -> not safely replayable.
        if re.search(r"'[^']*'", out) and literals:
            return None
        return out, spec

    def compile_template(self, task_id: str, question: str, sql: str) -> tuple[QueryTemplate | None, str]:
        literals = extract_literals(question)
        parsed = self._parameterise(sql, literals)
        if parsed is None:
            return None, "literal_not_substitutable"
        sql_template, spec = parsed
        # Replay check: the template bound with the original literals must
        # reproduce the original result.
        bound = bind_params(spec, literals) or {}
        replay = self.env.execute(sql_template, bound)
        original = self.env.execute(sql)
        if not replay["ok"] or replay["rows"] != original["rows"]:
            return None, "replay_mismatch"
        template = QueryTemplate(
            template_id=f"tpl_{uuid.uuid4().hex[:8]}",
            question_signature=question_signature(question),
            sql_template=sql_template,
            param_spec=spec,
            tables=self.env.tables_in_sql(sql),
            source_task_id=task_id,
        )
        return template, "ok"

    # -- Layer 2 --------------------------------------------------------

    def compile_exemplar(self, task_id: str, question: str, sql: str) -> SchemaExemplar:
        tables = self.env.tables_in_sql(sql)
        join_path = [jp for jp in self.env.join_paths() if all(t in tables for t in re.findall(r"(\w+)\.\w+", jp))]
        return SchemaExemplar(
            exemplar_id=f"exm_{uuid.uuid4().hex[:8]}",
            question=question,
            sql=sql,
            tables=tables,
            join_path=join_path,
            schema_fragment=self.env.schema_text(tables),
            source_task_id=task_id,
        )

    # -- Both -----------------------------------------------------------

    def compile(self, task_id: str, question: str, sql: str) -> CompileResult:
        template, reason = self.compile_template(task_id, question, sql)
        exemplar = self.compile_exemplar(task_id, question, sql)
        return CompileResult(template=template, exemplar=exemplar, reason=reason)
