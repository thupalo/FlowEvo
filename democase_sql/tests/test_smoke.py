"""Offline smoke tests (no API key required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from democase_sql.compiler import SqlCompiler, bind_params, extract_literals, question_signature  # noqa: E402
from democase_sql.db.build_db import build  # noqa: E402
from democase_sql.env import SqlEnvironment  # noqa: E402
from democase_sql.generator import OracleSqlGenerator, extract_sql  # noqa: E402
from democase_sql.runner import CONDITIONS, run_condition, summarize  # noqa: E402
from democase_sql.skill_library import SqlSkillLibrary  # noqa: E402
from democase_sql.tasks import build_tasks, shuffled  # noqa: E402


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    return build(tmp_path_factory.mktemp("db") / "demo.sqlite")


@pytest.fixture(scope="module")
def env(db_path: Path) -> SqlEnvironment:
    return SqlEnvironment(db_path)


def test_env_is_read_only(env: SqlEnvironment) -> None:
    assert not env.execute("DELETE FROM customers")["ok"]
    assert env.execute("SELECT COUNT(*) FROM customers")["rows"][0][0] == 40


def test_question_analysis() -> None:
    q = 'Get the address of the customer "Alice Smith".'
    assert extract_literals(q) == ["Alice Smith"]
    assert question_signature(q) == "get the address of the customer <p0>"
    spec = [{"name": "p0_0", "lit": 0, "part": 0}, {"name": "p0_1", "lit": 0, "part": 1}]
    assert bind_params(spec, ["Bob Nowak"]) == {"p0_0": "Bob", "p0_1": "Nowak"}
    assert bind_params(spec, ["Cher"]) is None


def test_extract_sql() -> None:
    assert extract_sql("Sure!\n```sql\nSELECT 1;\n```") == "SELECT 1"
    assert extract_sql("The query is: SELECT email FROM customers") == "SELECT email FROM customers"


def test_compile_address_template_and_replay(env: SqlEnvironment, db_path: Path) -> None:
    tasks = [t for t in build_tasks(db_path) if t.pattern == "customer_address"]
    first, second = tasks[0], tasks[1]
    compiler = SqlCompiler(env)
    result = compiler.compile(first.task_id, first.question, first.gold_sql)
    assert result.template is not None, result.reason
    assert ":p0_0" in result.template.sql_template and ":p0_1" in result.template.sql_template
    assert result.exemplar is not None and "addresses" in result.exemplar.tables

    lib = SqlSkillLibrary()
    assert lib.add_template(result.template)
    hit = lib.retrieve(second.question)
    assert hit["template"] is not None and hit["best_layer"] == 1
    bound = hit["params"]
    res = env.execute(hit["template"].sql_template, bound)
    assert res["ok"] and res["rows"] == env.execute(second.gold_sql)["rows"]


def test_ours_condition_reuses_templates(env: SqlEnvironment, db_path: Path, tmp_path: Path) -> None:
    tasks = shuffled(build_tasks(db_path), seed=3)
    gen = OracleSqlGenerator(env, fail_first_patterns={"customer_total_spent"})
    traces = run_condition("ours", tasks, env=env, generator=gen, output_dir=tmp_path)
    s = summarize(traces)
    assert s["pass_rate"] == 1.0
    assert s["direct_template_episodes"] >= 8  # 11 address tasks, first one compiles
    # At least one repair happened on the deliberately failing pattern
    assert any(len(t.attempts) > 1 for t in traces)
    assert (tmp_path / "ours" / "skill_library.json").exists()
    lib = SqlSkillLibrary.load(tmp_path / "ours" / "skill_library.json")
    assert lib.snapshot()["templates_active"] >= 5


def test_pure_dynamic_never_replays(env: SqlEnvironment, db_path: Path, tmp_path: Path) -> None:
    tasks = shuffled(build_tasks(db_path), seed=3)[:6]
    gen = OracleSqlGenerator(env)
    traces = run_condition("pure_dynamic", tasks, env=env, generator=gen, output_dir=tmp_path)
    assert all(t.planning_mode == "pure_dynamic" for t in traces)
    assert CONDITIONS["pure_dynamic"]["compile"] is False


def test_template_suppression() -> None:
    from democase_sql.schemas import QueryTemplate

    lib = SqlSkillLibrary()
    t = QueryTemplate(template_id="t1", question_signature="x <p0>", sql_template="SELECT :p0", param_spec=[{"name": "p0", "lit": 0, "part": -1}], tables=[], source_task_id="s")
    lib.add_template(t)
    lib.record_template_usage("t1", False)
    lib.record_template_usage("t1", False)
    assert lib.templates["t1"].status == "suppressed"
    assert lib.retrieve('x "v"')["template"] is None
