"""SQL environment: read-only execution, schema introspection, result comparison."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

_FORBIDDEN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


class SqlEnvironment:
    """Wraps a SQLite database opened read-only."""

    def __init__(self, db_path: str | Path, timeout_s: float = 5.0) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}. Run `python -m democase_sql.db.build_db`.")
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        self.con = sqlite3.connect(uri, uri=True, timeout=timeout_s)
        self.con.row_factory = None
        self._schema_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Schema introspection ("data structures")
    # ------------------------------------------------------------------

    def schema(self) -> dict[str, Any]:
        """Return {table: {"columns": [(name, type, pk)], "foreign_keys": [(col, ref_table, ref_col)]}}."""
        if self._schema_cache is not None:
            return self._schema_cache
        out: dict[str, Any] = {}
        tables = [
            r[0]
            for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        ]
        for t in tables:
            cols = [(r[1], r[2], bool(r[5])) for r in self.con.execute(f"PRAGMA table_info('{t}')")]
            fks = [(r[3], r[2], r[4]) for r in self.con.execute(f"PRAGMA foreign_key_list('{t}')")]
            out[t] = {"columns": cols, "foreign_keys": fks}
        self._schema_cache = out
        return out

    def schema_text(self, tables: list[str] | None = None) -> str:
        """Compact DDL-like schema description for prompts."""
        schema = self.schema()
        lines: list[str] = []
        for t, info in schema.items():
            if tables and t not in tables:
                continue
            cols = ", ".join(f"{n} {ty}{' PK' if pk else ''}" for n, ty, pk in info["columns"])
            lines.append(f"TABLE {t} ({cols})")
            for col, rt, rc in info["foreign_keys"]:
                lines.append(f"  FK {t}.{col} -> {rt}.{rc}")
        return "\n".join(lines)

    def join_paths(self) -> list[str]:
        """All FK relations as 'a.col -> b.col' strings."""
        out: list[str] = []
        for t, info in self.schema().items():
            for col, rt, rc in info["foreign_keys"]:
                out.append(f"{t}.{col} -> {rt}.{rc}")
        return out

    def tables_in_sql(self, sql: str) -> list[str]:
        names = set(self.schema())
        found = {tok for tok in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", sql) if tok in names}
        return sorted(found)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a read-only query. Returns {ok, rows, columns, error}."""
        sql = (sql or "").strip().rstrip(";").strip()
        if not sql:
            return {"ok": False, "rows": [], "columns": [], "error": "Empty SQL."}
        if _FORBIDDEN.match(sql):
            return {"ok": False, "rows": [], "columns": [], "error": "Only SELECT statements are allowed."}
        try:
            cur = self.con.execute(sql, params or {})
            rows = cur.fetchmany(1000)
            columns = [d[0] for d in (cur.description or [])]
            return {"ok": True, "rows": [tuple(r) for r in rows], "columns": columns, "error": ""}
        except (sqlite3.Error, sqlite3.Warning) as exc:
            # sqlite3.Warning: e.g. "You can only execute one statement at a time"
            return {"ok": False, "rows": [], "columns": [], "error": f"{type(exc).__name__}: {exc}"}
        except (OverflowError, ValueError, TypeError) as exc:
            # bad parameter binding / value conversion
            return {"ok": False, "rows": [], "columns": [], "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, str):
            return v.strip()
        return v

    def compare(self, candidate_sql: str, gold_sql: str) -> tuple[bool, str]:
        """Execute both and compare result multisets (order-insensitive unless
        the gold query has ORDER BY).  Column names are ignored; column
        order and values must match."""
        gold = self.execute(gold_sql)
        if not gold["ok"]:
            return False, f"gold query failed: {gold['error']}"
        cand = self.execute(candidate_sql)
        if not cand["ok"]:
            return False, f"execution error: {cand['error']}"

        g_rows = [tuple(self._norm(v) for v in r) for r in gold["rows"]]
        c_rows = [tuple(self._norm(v) for v in r) for r in cand["rows"]]
        ordered = re.search(r"\bORDER\s+BY\b", gold_sql, re.IGNORECASE) is not None
        if not ordered:
            g_rows, c_rows = sorted(g_rows, key=repr), sorted(c_rows, key=repr)
        if g_rows == c_rows:
            return True, "correct"
        if len(g_rows) != len(c_rows):
            return False, f"row count mismatch: expected {len(g_rows)}, got {len(c_rows)}; got columns={cand['columns']}"
        if g_rows and len(g_rows[0]) != len(c_rows[0]):
            return False, f"column count mismatch: expected {len(g_rows[0])}, got {len(c_rows[0])}; got columns={cand['columns']}"
        return False, f"value mismatch: expected first row {g_rows[0] if g_rows else ()}, got {c_rows[0] if c_rows else ()}"

    def close(self) -> None:
        self.con.close()
