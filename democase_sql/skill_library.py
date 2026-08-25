"""Three-layer skill library for SQL tasks with governance.

Layer 1  QueryTemplate   — direct replay, zero LLM tokens, matched by question signature
Layer 2  SchemaExemplar  — few-shot (question, sql, schema fragment) context
Layer 3  SchemaInsight   — aggregated join/table statistics + pitfalls

Governance:
* template utility tracking + suppression on repeated failure
* contrastive evaluation of injected layers (guided vs. unguided holdout)
  per signature cluster; injection is masked when it measurably hurts.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .compiler import bind_params, extract_literals, question_signature, signature_tokens
from .schemas import QueryTemplate, SchemaExemplar, SchemaInsight


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


class SqlSkillLibrary:
    # Layer 1 routing
    DIRECT_MIN_SIMILARITY = 0.85
    # Governance thresholds
    TEMPLATE_SUPPRESS_FAILURES = 2
    TEMPLATE_SUPPRESS_UTILITY = 0.5
    EXEMPLAR_TOP_K = 2
    EXEMPLAR_MIN_SIMILARITY = 0.15
    INSIGHT_MIN_SAMPLES = 3
    CONTRASTIVE_MIN_GUIDED = 4
    CONTRASTIVE_MIN_UNGUIDED = 2
    CONTRASTIVE_HARM_THRESHOLD = -0.1
    HOLDOUT_EVERY = 5  # every Nth seeded episode of a cluster runs unguided

    def __init__(self, governance_enabled: bool = True) -> None:
        self.governance_enabled = governance_enabled
        self.templates: dict[str, QueryTemplate] = {}
        self.exemplars: dict[str, SchemaExemplar] = {}
        self.insight = SchemaInsight()
        self._episode = 0
        self._stats: list[dict[str, Any]] = []  # per-trace stats feeding Layer 3
        self._contrastive: dict[str, dict[str, int]] = {}  # cluster -> counts
        self._injection_suppressed: set[str] = set()

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def add_template(self, t: QueryTemplate) -> bool:
        """One active template per signature; a new one replaces an existing
        template only when that one is suppressed or low-utility."""
        for existing in self.templates.values():
            if existing.question_signature == t.question_signature:
                if existing.status == "active" and existing.utility >= self.TEMPLATE_SUPPRESS_UTILITY:
                    return False
                del self.templates[existing.template_id]
                break
        self.templates[t.template_id] = t
        return True

    def add_exemplar(self, e: SchemaExemplar) -> None:
        # Deduplicate on signature: keep the most recent per signature (max 3)
        sig = question_signature(e.question)
        same = [x for x in self.exemplars.values() if question_signature(x.question) == sig]
        if len(same) >= 3:
            del self.exemplars[same[0].exemplar_id]
        self.exemplars[e.exemplar_id] = e

    def record_trace_stats(self, *, success: bool, tables: list[str], join_path: list[str], feedback: str) -> None:
        pitfall = ""
        if not success:
            if feedback.startswith("execution error"):
                pitfall = "execution_error"
            elif "column count" in feedback:
                pitfall = "wrong_columns"
            elif "row count" in feedback:
                pitfall = "wrong_rows"
            elif "value mismatch" in feedback:
                pitfall = "wrong_values"
            else:
                pitfall = "other"
        self._stats.append({"success": success, "tables": tables, "join_path": join_path, "pitfall": pitfall})
        if len(self._stats) >= self.INSIGHT_MIN_SAMPLES:
            self._rebuild_insight()

    def _rebuild_insight(self) -> None:
        tf: Counter[str] = Counter()
        jf: Counter[str] = Counter()
        pf: Counter[str] = Counter()
        for s in self._stats:
            if s["success"]:
                tf.update(s["tables"])
                jf.update(s["join_path"])
            elif s["pitfall"]:
                pf[s["pitfall"]] += 1
        total_fail = sum(pf.values())
        pitfalls: list[str] = []
        msgs = {
            "execution_error": "of failures were SQL execution errors -- check column/table names against the schema",
            "wrong_columns": "of failures returned the wrong number of columns -- select exactly what the question asks",
            "wrong_rows": "of failures returned the wrong number of rows -- check WHERE filters and joins for duplicates",
            "wrong_values": "of failures had wrong values -- check aggregation and join keys",
            "other": "of failures had no clear pattern",
        }
        for reason, n in pf.most_common(3):
            pitfalls.append(f"{100 * n / total_fail:.0f}% {msgs[reason]}")
        self.insight = SchemaInsight(
            join_frequency=dict(jf), table_frequency=dict(tf), common_pitfalls=pitfalls, sample_count=len(self._stats)
        )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def cluster_of(self, question: str) -> str:
        return question_signature(question)

    def retrieve(self, question: str) -> dict[str, Any]:
        """Return the best available layers for *question*.

        Keys: template, params, exemplars, insight, best_layer, injection_blocked.
        """
        sig = self.cluster_of(question)
        literals = extract_literals(question)
        sig_tok = signature_tokens(sig)

        # Layer 1
        template: QueryTemplate | None = None
        params: dict[str, Any] | None = None
        best_sim = 0.0
        for t in self.templates.values():
            if t.status != "active":
                continue
            sim = 1.0 if t.question_signature == sig else _jaccard(sig_tok, signature_tokens(t.question_signature))
            if sim < self.DIRECT_MIN_SIMILARITY or sim < best_sim:
                continue
            bound = bind_params(t.param_spec, literals)
            if bound is None:
                continue
            template, params, best_sim = t, bound, sim

        # Layers 2/3 (maskable by contrastive governance)
        blocked = self.governance_enabled and sig in self._injection_suppressed
        exemplars: list[SchemaExemplar] = []
        if not blocked:
            scored = []
            for e in self.exemplars.values():
                sim = _jaccard(sig_tok, signature_tokens(question_signature(e.question)))
                if sim >= self.EXEMPLAR_MIN_SIMILARITY:
                    scored.append((sim, e))
            scored.sort(key=lambda x: -x[0])
            exemplars = [e for _, e in scored[: self.EXEMPLAR_TOP_K]]
        insight = None if blocked or self.insight.sample_count < self.INSIGHT_MIN_SAMPLES else self.insight

        best = 1 if template else 2 if exemplars else 3 if insight else None
        return {
            "template": template,
            "params": params,
            "template_similarity": best_sim,
            "exemplars": exemplars,
            "insight": insight,
            "best_layer": best,
            "injection_blocked": blocked,
        }

    # ------------------------------------------------------------------
    # Governance
    # ------------------------------------------------------------------

    def advance_episode(self) -> None:
        self._episode += 1

    def record_template_usage(self, template_id: str, success: bool) -> None:
        t = self.templates.get(template_id)
        if t is None:
            return
        t.use_count += 1
        t.success_count += int(success)
        t.failure_count += int(not success)
        t.utility = t.success_count / max(t.use_count, 1)
        t.last_used_episode = self._episode
        if (
            self.governance_enabled
            and t.failure_count >= self.TEMPLATE_SUPPRESS_FAILURES
            and t.utility < self.TEMPLATE_SUPPRESS_UTILITY
        ):
            t.status = "suppressed"

    def should_holdout(self, cluster: str) -> bool:
        """Decide whether this seeded episode should run *unguided* so the
        contrastive evaluator gets a baseline sample."""
        if not self.governance_enabled:
            return False
        c = self._contrastive.setdefault(cluster, {"gs": 0, "gt": 0, "us": 0, "ut": 0})
        return (c["gt"] + c["ut"]) % self.HOLDOUT_EVERY == self.HOLDOUT_EVERY - 1

    def record_guided(self, cluster: str, success: bool) -> None:
        c = self._contrastive.setdefault(cluster, {"gs": 0, "gt": 0, "us": 0, "ut": 0})
        c["gt"] += 1
        c["gs"] += int(success)
        self._check_contrastive(cluster)

    def record_unguided(self, cluster: str, success: bool) -> None:
        c = self._contrastive.setdefault(cluster, {"gs": 0, "gt": 0, "us": 0, "ut": 0})
        c["ut"] += 1
        c["us"] += int(success)
        self._check_contrastive(cluster)

    def contrastive_delta(self, cluster: str) -> float | None:
        c = self._contrastive.get(cluster)
        if not c or c["gt"] < self.CONTRASTIVE_MIN_GUIDED or c["ut"] < self.CONTRASTIVE_MIN_UNGUIDED:
            return None
        return c["gs"] / c["gt"] - c["us"] / c["ut"]

    def _check_contrastive(self, cluster: str) -> None:
        if not self.governance_enabled:
            return
        d = self.contrastive_delta(cluster)
        if d is not None and d < self.CONTRASTIVE_HARM_THRESHOLD:
            self._injection_suppressed.add(cluster)

    # ------------------------------------------------------------------
    # Persistence / reporting
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "templates_total": len(self.templates),
            "templates_active": sum(1 for t in self.templates.values() if t.status == "active"),
            "templates_suppressed": sum(1 for t in self.templates.values() if t.status == "suppressed"),
            "exemplars_total": len(self.exemplars),
            "insight_samples": self.insight.sample_count,
            "injection_suppressed_clusters": len(self._injection_suppressed),
            "episode": self._episode,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "governance_enabled": self.governance_enabled,
            "templates": {k: v.to_dict() for k, v in self.templates.items()},
            "exemplars": {k: v.to_dict() for k, v in self.exemplars.items()},
            "insight": self.insight.to_dict(),
            "episode": self._episode,
            "stats": self._stats,
            "contrastive": self._contrastive,
            "injection_suppressed": sorted(self._injection_suppressed),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SqlSkillLibrary":
        lib = cls(governance_enabled=bool(d.get("governance_enabled", True)))
        lib.templates = {k: QueryTemplate.from_dict(v) for k, v in d.get("templates", {}).items()}
        lib.exemplars = {k: SchemaExemplar.from_dict(v) for k, v in d.get("exemplars", {}).items()}
        lib.insight = SchemaInsight.from_dict(d.get("insight", {}))
        lib._episode = int(d.get("episode", 0))
        lib._stats = list(d.get("stats", []))
        lib._contrastive = {k: dict(v) for k, v in d.get("contrastive", {}).items()}
        lib._injection_suppressed = set(d.get("injection_suppressed", []))
        return lib

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SqlSkillLibrary":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
