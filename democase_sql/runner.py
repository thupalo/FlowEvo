"""Episode runner for the SQL demo case.

Conditions (parallel to the ALFWorld / code-math runners):

* ``pure_dynamic``   — LLM generates SQL from the schema every time, no memory
* ``compile_only``   — compile skills but never use them (measures overhead)
* ``layer1_only``    — direct template replay only
* ``full_library``   — L1 replay + L2 exemplars + L3 insights, no retry
* ``ours``           — full_library + verifier-driven repair (2 rounds) + governance
* ``no_governance``  — ``ours`` with governance switched off

Usage::

    python -m democase_sql.runner --generator oracle --output-dir democase_sql/runs/oracle_demo
    python -m democase_sql.runner --generator llm --config-path configs/default.yaml \
        --output-dir democase_sql/runs/llm_demo --conditions pure_dynamic ours
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import sys

from . import _SRC  # noqa: F401  (ensures FlowEvo src is importable)
from .compiler import SqlCompiler
from .env import SqlEnvironment
from .errors import ConfigError, LLMGenerationError, RunAborted
from .generator import LLMSqlGenerator, OracleSqlGenerator
from .schemas import SqlTask, SqlTrace
from .skill_library import SqlSkillLibrary
from .tasks import build_tasks, shuffled

CONDITIONS: dict[str, dict[str, Any]] = {
    "pure_dynamic": {"display": "Pure dynamic (schema + LLM, no memory)", "compile": False, "layer1": False, "layer23": False, "retry": 0, "governance": False},
    "compile_only": {"display": "Compile only (build library, never reuse)", "compile": True, "layer1": False, "layer23": False, "retry": 0, "governance": False},
    "layer1_only": {"display": "Layer 1 only (direct template replay)", "compile": True, "layer1": True, "layer23": False, "retry": 0, "governance": True},
    "full_library": {"display": "Full library (L1 + L2 + L3), no retry", "compile": True, "layer1": True, "layer23": True, "retry": 0, "governance": True},
    "ours": {"display": "Ours (full library + repair + governance)", "compile": True, "layer1": True, "layer23": True, "retry": 2, "governance": True},
    "no_governance": {"display": "Ours without governance", "compile": True, "layer1": True, "layer23": True, "retry": 2, "governance": False},
}


# ----------------------------------------------------------------------
# One episode
# ----------------------------------------------------------------------

def run_episode(task: SqlTask, *, condition: str, cfg: dict[str, Any], env: SqlEnvironment, generator, library: SqlSkillLibrary | None, compiler: SqlCompiler | None, schema_text: str) -> SqlTrace:
    t0 = time.perf_counter()
    trace = SqlTrace(task_id=task.task_id, question=task.question, condition=condition, planning_mode="pure_dynamic")
    cluster = library.cluster_of(task.question) if library else ""

    retrieved: dict[str, Any] = {"template": None, "exemplars": [], "insight": None}
    if library is not None and (cfg["layer1"] or cfg["layer23"]):
        library.advance_episode()
        retrieved = library.retrieve(task.question)

    # ---- Layer 1: direct replay (zero LLM tokens) -----------------------
    template = retrieved["template"] if cfg["layer1"] else None
    if template is not None:
        trace.planning_mode = "direct_template"
        trace.template_id = template.template_id
        sql = template.sql_template
        params = retrieved["params"] or {}
        res = env.execute(sql, params)
        if res["ok"]:
            # Compare against gold by executing the bound template through the verifier
            bound_sql = sql
            for k, v in params.items():
                bound_sql = bound_sql.replace(f":{k}", "'" + str(v).replace("'", "''") + "'")
            ok, fb = env.compare(bound_sql, task.gold_sql)
        else:
            ok, fb = False, f"execution error: {res['error']}"
        trace.attempts.append({"stage": "direct_template", "sql": sql, "params": params, "passed": ok, "feedback": fb})
        library.record_template_usage(template.template_id, ok)
        if ok:
            trace.final_sql, trace.success, trace.feedback = sql, True, fb
            trace.latency_ms = (time.perf_counter() - t0) * 1000
            return trace
        # fall through to generation with the failure as feedback
        trace.feedback = fb

    # ---- Layers 2/3: seeded generation ----------------------------------
    exemplars, insight = [], None
    guided = False
    holdout = False
    if cfg["layer23"] and library is not None:
        exemplars, insight = retrieved["exemplars"], retrieved["insight"]
        if exemplars or insight:
            if library.should_holdout(cluster):
                holdout = True
                exemplars, insight = [], None
            else:
                guided = True
    if guided:
        trace.planning_mode = "skill_seeded"
        trace.exemplar_ids = [e.exemplar_id for e in exemplars]
        trace.insight_used = insight is not None
    elif trace.planning_mode != "direct_template":
        trace.planning_mode = "pure_dynamic"

    def _account(out) -> None:
        trace.llm_calls += 1
        trace.prompt_tokens += out.prompt_tokens
        trace.completion_tokens += out.completion_tokens
        trace.budget_retries += out.budget_retries
        trace.context_shrinks += out.context_shrinks

    sql, ok, fb = "", False, ""
    try:
        out = generator.draft(task, schema_text=schema_text, exemplars=exemplars, insight=insight)
        _account(out)
        ok, fb = env.compare(out.sql, task.gold_sql)
        trace.attempts.append({"stage": "draft", "sql": out.sql, "passed": ok, "feedback": fb})
        sql = out.sql

        for r in range(int(cfg["retry"])):
            if ok:
                break
            out = generator.repair(task, schema_text=schema_text, previous_sql=sql, feedback=fb, exemplars=exemplars, insight=insight)
            _account(out)
            ok, fb = env.compare(out.sql, task.gold_sql)
            sql = out.sql
            trace.attempts.append({"stage": f"repair_{r + 1}", "sql": sql, "passed": ok, "feedback": fb})
    except LLMGenerationError as exc:
        # Fatal kinds (auth, bad request) must stop the run; everything else
        # is recorded on the trace and the loop moves on.
        if exc.fatal:
            raise
        ok = False
        fb = f"llm error: {exc}"
        trace.attempts.append({"stage": "llm_error", "sql": sql, "passed": False, "feedback": fb})
        trace.failure_type = exc.kind
        trace.budget_retries += 0 if exc.kind != "output_budget_exhausted" else LLMSqlGenerator.MAX_BUDGET_GROWTH_STEPS

    trace.final_sql, trace.success, trace.feedback = sql, ok, fb
    if not ok and not trace.failure_type:
        trace.failure_type = classify_feedback(fb)

    # ---- Governance bookkeeping ------------------------------------------
    if library is not None and cfg["layer23"]:
        if guided:
            library.record_guided(cluster, ok)
        elif holdout:
            library.record_unguided(cluster, ok)

    # ---- Compile ---------------------------------------------------------
    if library is not None and compiler is not None:
        tables = env.tables_in_sql(sql)
        library.record_trace_stats(success=ok, tables=tables, join_path=[], feedback=fb)
        if ok and cfg["compile"]:
            try:
                result = compiler.compile(task.task_id, task.question, sql)
            except Exception as exc:  # noqa: BLE001 — a compile bug must not lose a solved episode
                trace.feedback += f" | compile skipped: {type(exc).__name__}: {str(exc)[:120]}"
            else:
                if result.template is not None and library.add_template(result.template):
                    trace.compiled_template_id = result.template.template_id
                if result.exemplar is not None:
                    library.add_exemplar(result.exemplar)
                    trace.compiled_exemplar_id = result.exemplar.exemplar_id
                    # join path stats are only known post-compile
                    library._stats[-1]["join_path"] = result.exemplar.join_path

    trace.latency_ms = (time.perf_counter() - t0) * 1000
    return trace


def classify_feedback(feedback: str) -> str:
    """Map verifier feedback to a coarse failure category."""
    if feedback.startswith("execution error"):
        return "execution_error"
    if "column count" in feedback:
        return "wrong_columns"
    if "row count" in feedback:
        return "wrong_rows"
    if "value mismatch" in feedback:
        return "wrong_values"
    if feedback.startswith("gold query failed"):
        return "bad_gold"
    return "other" if feedback else ""


# ----------------------------------------------------------------------
# Condition loop + report
# ----------------------------------------------------------------------

MAX_CONSECUTIVE_ERRORS = 3


def run_condition(
    condition: str,
    tasks: list[SqlTask],
    *,
    env: SqlEnvironment,
    generator,
    output_dir: Path,
    max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS,
) -> list[SqlTrace]:
    """Run all tasks under one condition.

    Episode-level policy:
    * verifier failures and *retryable* LLM errors are recorded and the loop continues;
    * a *fatal* LLM error (auth, bad request) or ``max_consecutive_errors``
      LLM/unexpected errors in a row raise ``RunAborted`` — partial results are
      still written to disk first.
    """
    cfg = CONDITIONS[condition]
    print("\n" + "=" * 78)
    print(f"  {condition} — {cfg['display']}")
    print("=" * 78)
    library = SqlSkillLibrary(governance_enabled=cfg["governance"]) if (cfg["compile"] or cfg["layer1"] or cfg["layer23"]) else None
    compiler = SqlCompiler(env) if library is not None else None
    schema_text = env.schema_text()
    if hasattr(generator, "_failed_once"):
        generator._failed_once.clear()

    traces: list[SqlTrace] = []
    cum = 0
    consecutive_errors = 0
    abort_reason = ""

    def _persist() -> None:
        cond_dir = output_dir / condition
        cond_dir.mkdir(parents=True, exist_ok=True)
        (cond_dir / "episodes.json").write_text(json.dumps([t.to_dict() for t in traces], indent=2, ensure_ascii=False), encoding="utf-8")
        if library is not None:
            library.save(cond_dir / "skill_library.json")

    for i, task in enumerate(tasks):
        try:
            tr = run_episode(task, condition=condition, cfg=cfg, env=env, generator=generator, library=library, compiler=compiler, schema_text=schema_text)
        except LLMGenerationError as exc:
            # Only fatal kinds propagate out of run_episode.
            abort_reason = f"fatal LLM error on {task.task_id}: {exc}"
            break
        except KeyboardInterrupt:
            abort_reason = "interrupted by user"
            break
        except Exception as exc:  # noqa: BLE001 — unexpected bug in one episode
            tr = SqlTrace(task_id=task.task_id, question=task.question, condition=condition, planning_mode="pure_dynamic")
            tr.feedback = f"unexpected error: {type(exc).__name__}: {str(exc)[:300]}"
            tr.failure_type = "unexpected_error"
            if library is not None:
                library.record_trace_stats(success=False, tables=[], join_path=[], feedback=tr.feedback)

        traces.append(tr)
        cum += int(tr.success)
        mode = {"direct_template": "L1", "skill_seeded": "L2/3", "pure_dynamic": "dyn"}[tr.planning_mode]
        status = "PASS" if tr.success else ("ERR " if tr.failure_type in ("unexpected_error",) or tr.feedback.startswith("llm error") else "FAIL")
        extra = ""
        if tr.budget_retries:
            extra += f" budget+{tr.budget_retries}"
        if tr.context_shrinks:
            extra += f" shrink{tr.context_shrinks}"
        if not tr.success:
            extra += f" [{tr.failure_type}] {tr.feedback[:70]}"
        print(
            f"  [{i + 1:2d}/{len(tasks)}] {status} {task.task_id} {task.pattern:24s} "
            f"mode={mode:4s} calls={tr.llm_calls} tok={tr.total_tokens:5d} "
            f"lib={library.snapshot()['templates_active'] if library else 0:2d} cum={100 * cum / (i + 1):5.1f}%{extra}"
        )

        is_error = status == "ERR "
        consecutive_errors = consecutive_errors + 1 if is_error else 0
        if consecutive_errors >= max_consecutive_errors:
            abort_reason = f"{consecutive_errors} consecutive errors (last: {tr.feedback[:120]})"
            break

    _persist()
    if abort_reason:
        raise RunAborted(f"{condition}: {abort_reason}; {len(traces)}/{len(tasks)} episodes saved to {output_dir / condition}")
    return traces


def summarize(traces: list[SqlTrace]) -> dict[str, Any]:
    n = len(traces)
    modes = {"direct_template": 0, "skill_seeded": 0, "pure_dynamic": 0}
    for t in traces:
        modes[t.planning_mode] += 1
    return {
        "n": n,
        "passed": sum(t.success for t in traces),
        "pass_rate": round(sum(t.success for t in traces) / max(n, 1), 4),
        "first_attempt_pass_rate": round(sum(1 for t in traces if t.attempts and t.attempts[0]["passed"]) / max(n, 1), 4),
        "llm_calls": sum(t.llm_calls for t in traces),
        "total_tokens": sum(t.total_tokens for t in traces),
        "avg_tokens": round(sum(t.total_tokens for t in traces) / max(n, 1), 1),
        "direct_template_episodes": modes["direct_template"],
        "skill_seeded_episodes": modes["skill_seeded"],
        "pure_dynamic_episodes": modes["pure_dynamic"],
        "compiled_templates": sum(1 for t in traces if t.compiled_template_id),
        "avg_latency_ms": round(sum(t.latency_ms for t in traces) / max(n, 1), 1),
        "budget_retries": sum(t.budget_retries for t in traces),
        "context_shrinks": sum(t.context_shrinks for t in traces),
        "llm_errors": sum(1 for t in traces if t.feedback.startswith("llm error") or t.failure_type == "unexpected_error"),
        "failure_types": dict(sorted(Counter(t.failure_type for t in traces if not t.success).items())),
    }


def write_report(results: dict[str, list[SqlTrace]], output_dir: Path, generator_name: str) -> Path:
    lines = ["# FlowEvo SQL demo — run report", "", f"Generator: `{generator_name}`  ", f"Tasks per condition: {len(next(iter(results.values())))}", ""]
    lines.append("| condition | pass | 1st-try | LLM calls | tokens | avg tok | L1 | L2/3 | dyn | compiled | budget↑ | shrink | errors | failure types |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cond, traces in results.items():
        s = summarize(traces)
        ft = ", ".join(f"{k}:{v}" for k, v in s["failure_types"].items()) or "–"
        lines.append(
            f"| {cond} | {s['passed']}/{s['n']} ({100 * s['pass_rate']:.0f}%) | {100 * s['first_attempt_pass_rate']:.0f}% | {s['llm_calls']} | {s['total_tokens']} | {s['avg_tokens']} | "
            f"{s['direct_template_episodes']} | {s['skill_seeded_episodes']} | {s['pure_dynamic_episodes']} | {s['compiled_templates']} | "
            f"{s['budget_retries']} | {s['context_shrinks']} | {s['llm_errors']} | {ft} |"
        )
    lines += [
        "",
        "L1 = direct template replay (no LLM), L2/3 = exemplar/insight-seeded generation, dyn = pure dynamic. "
        "budget↑ = output-token budget increases (reasoning models), shrink = skill context dropped for context length, "
        "errors = episodes lost to LLM/runtime errors.",
        "",
    ]
    for cond, traces in results.items():
        lines.append(f"## {cond}")
        lines.append("")
        lines.append("| # | task | mode | calls | tokens | result | note |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, t in enumerate(traces):
            note = "compiled " + t.compiled_template_id if t.compiled_template_id else ("replayed " + t.template_id if t.template_id else "")
            if not t.success:
                note = (note + " " if note else "") + f"[{t.failure_type}] {t.feedback[:80].replace('|', '/')}"
            lines.append(f"| {i + 1} | {t.task_id} | {t.planning_mode} | {t.llm_calls} | {t.total_tokens} | {'PASS' if t.success else 'FAIL'} | {note} |")
        lines.append("")
    path = output_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="FlowEvo SQL demo runner")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "db" / "demo.sqlite"))
    ap.add_argument("--output-dir", default=str(Path(__file__).resolve().parent / "runs" / "latest"))
    ap.add_argument("--generator", choices=["oracle", "llm"], default="oracle")
    ap.add_argument("--config-path", default="configs/default.yaml")
    ap.add_argument("--model", default=None)
    ap.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="LLM output budget per call. Reasoning models need room for hidden thinking (default 4096).",
    )
    ap.add_argument("--conditions", nargs="+", default=["pure_dynamic", "ours"], choices=sorted(CONDITIONS))
    ap.add_argument("--seed", type=int, default=3, help="task order seed")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sys.exit(run(args))


def run(args: argparse.Namespace) -> int:
    """Execute the CLI; returns a process exit code (0 ok, 1 aborted, 2 config)."""
    db_path = Path(args.db)
    if not db_path.exists():
        from .db.build_db import build

        print(f"Building demo database at {db_path} ...")
        build(db_path)
    try:
        env = SqlEnvironment(db_path)
    except (FileNotFoundError, OSError) as exc:
        print(f"error: cannot open database: {exc}", file=sys.stderr)
        return 2
    tasks = shuffled(build_tasks(db_path), seed=args.seed)
    if args.limit:
        tasks = tasks[: args.limit]

    try:
        if args.generator == "oracle":
            generator = OracleSqlGenerator(env, fail_first_patterns={"customer_total_spent", "top_customers_by_spend"})
        else:
            generator = LLMSqlGenerator(config_path=args.config_path, model=args.model, max_output_tokens=args.max_output_tokens)
            print(f"LLM: {generator.cfg.provider} / {generator.cfg.model} @ {generator.cfg.base_url}  (max_output_tokens={generator.max_output_tokens})")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: set base_url / model / api_key in configs/local.yaml or export OPENROUTER_API_KEY.", file=sys.stderr)
        env.close()
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tasks.json").write_text(json.dumps([t.to_dict() for t in tasks], indent=2, ensure_ascii=False), encoding="utf-8")

    results: dict[str, list[SqlTrace]] = {}
    exit_code = 0
    try:
        for cond in args.conditions:
            results[cond] = run_condition(cond, tasks, env=env, generator=generator, output_dir=output_dir)
    except RunAborted as exc:
        print(f"\nRUN ABORTED — {exc}", file=sys.stderr)
        exit_code = 1
        # include whatever this condition managed to save so the report is still useful
        partial = output_dir / str(exc).split(":", 1)[0] / "episodes.json"
        if partial.exists():
            cond = partial.parent.name
            raw = json.loads(partial.read_text(encoding="utf-8"))
            results[cond] = [SqlTrace(**{k: v for k, v in d.items() if k != "total_tokens"}) for d in raw]
    finally:
        env.close()

    if results:
        report = write_report(results, output_dir, args.generator)
        print("\nSummary:")
        for cond, traces in results.items():
            s = summarize(traces)
            print(
                f"  {cond:14s} pass={s['passed']}/{s['n']}  calls={s['llm_calls']:3d}  tokens={s['total_tokens']:6d}  "
                f"L1={s['direct_template_episodes']}  L2/3={s['skill_seeded_episodes']}  dyn={s['pure_dynamic_episodes']}  "
                f"budget+={s['budget_retries']}  shrink={s['context_shrinks']}  errors={s['llm_errors']}"
            )
        print(f"\nReport: {report}")
    return exit_code


if __name__ == "__main__":
    main()
