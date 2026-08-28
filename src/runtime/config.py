"""Load and validate runtime LLM configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RuntimeConfigError(RuntimeError):
    """Raised when runtime configuration is missing or invalid."""


# Both names select the OpenAI chat-completions protocol. `openrouter` is the
# historical name; `openai_compatible` makes it explicit that any compatible
# server (vLLM, llama.cpp, LM Studio, Ollama, Azure/OpenAI proxies) works by
# pointing `base_url` at it.
SUPPORTED_PROVIDERS = {"openrouter", "openai_compatible"}
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def is_openrouter_endpoint(base_url: str) -> bool:
    return "openrouter.ai" in str(base_url or "").lower()


@dataclass(frozen=True)
class GenerationSettings:
    temperature: float
    max_output_tokens: int


@dataclass(frozen=True)
class SkillContextBudgets:
    draft_total_tokens: int
    repair_total_tokens: int
    draft_per_skill_tokens: int
    repair_per_skill_tokens: int


@dataclass(frozen=True)
class AlfWorldGenerationBudgets:
    """Output-token budgets used by the ALFWorld adapter.

    Defaults are the values the paper experiments used; raise them for
    reasoning backbones that spend tokens on hidden thinking.
    """

    step_max_output_tokens: int = 256
    compile_max_output_tokens: int = 500
    strategy_max_output_tokens: int = 200


def alfworld_budgets(llm_client: Any) -> AlfWorldGenerationBudgets:
    """Budgets from an LLM client's config, or the defaults when unavailable
    (e.g. a stub client in tests)."""
    budgets = getattr(getattr(llm_client, "config", None), "alfworld", None)
    return budgets if isinstance(budgets, AlfWorldGenerationBudgets) else AlfWorldGenerationBudgets()


@dataclass(frozen=True)
class RuntimeLLMConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    app_name: str
    skill_top_k: int
    skill_context_budgets: SkillContextBudgets
    draft: GenerationSettings
    repair: GenerationSettings
    config_path: str
    local_override_path: str
    # Opt-in: when a reply comes back with empty content and
    # finish_reason == "length" (reasoning models exhausting max_tokens while
    # thinking), retry with the output budget doubled, up to the cap.
    # Default off so published experiment numbers are unaffected.
    grow_on_truncation: bool = False
    max_output_tokens_cap: int = 16384
    # Optional `llm.alfworld` block; see AlfWorldGenerationBudgets.
    alfworld: AlfWorldGenerationBudgets = AlfWorldGenerationBudgets()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"Expected mapping in config file: {path}")
    return data


def _merge_nested(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_path(root: Path, config_dir: Path, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute() and path.exists():
        return path.resolve()
    if not path.is_absolute():
        path = (config_dir / path) if path.parent == Path(".") else (root / path)
    return path


def _env_overrides() -> dict[str, Any]:
    llm: dict[str, Any] = {}
    provider = os.getenv("FLOWEVO_PROVIDER")
    model = os.getenv("FLOWEVO_MODEL")
    base_url = os.getenv("FLOWEVO_BASE_URL")
    app_name = os.getenv("FLOWEVO_APP_NAME")
    skill_top_k = os.getenv("FLOWEVO_SKILL_TOP_K")
    if provider:
        llm["provider"] = provider
    if model:
        llm["model"] = model
    if base_url:
        llm["base_url"] = base_url
    if app_name:
        llm["app_name"] = app_name
    if skill_top_k:
        llm["skill_top_k"] = int(skill_top_k)
    return {"llm": llm} if llm else {}


def load_runtime_config(
    *,
    config_path: str = "configs/default.yaml",
    local_override_path: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    skill_top_k: int | None = None,
    draft_total_tokens: int | None = None,
    repair_total_tokens: int | None = None,
    draft_per_skill_tokens: int | None = None,
    repair_per_skill_tokens: int | None = None,
) -> RuntimeLLMConfig:
    config_file = Path(config_path)
    config_dir = config_file.resolve().parent
    root = config_file.resolve().parent.parent
    defaults = _read_yaml(config_file)
    llm_defaults = defaults.get("llm")
    if not isinstance(llm_defaults, dict):
        raise RuntimeConfigError("Missing `llm` mapping in runtime config.")

    inferred_override = local_override_path or str(llm_defaults.get("local_override_path", "")).strip()
    override_path = _resolve_path(root, config_dir, inferred_override)
    override_data = _read_yaml(override_path) if override_path is not None else {}
    merged = _merge_nested(defaults, _env_overrides())
    merged = _merge_nested(merged, override_data)

    explicit_overrides: dict[str, Any] = {"llm": {}}
    if provider is not None:
        explicit_overrides["llm"]["provider"] = provider
    if model is not None:
        explicit_overrides["llm"]["model"] = model
    if base_url is not None:
        explicit_overrides["llm"]["base_url"] = base_url
    if skill_top_k is not None:
        explicit_overrides["llm"]["skill_top_k"] = skill_top_k
    if draft_total_tokens is not None:
        explicit_overrides["llm"].setdefault("skill_context_budgets", {})["draft_total_tokens"] = draft_total_tokens
    if repair_total_tokens is not None:
        explicit_overrides["llm"].setdefault("skill_context_budgets", {})["repair_total_tokens"] = repair_total_tokens
    if draft_per_skill_tokens is not None:
        explicit_overrides["llm"].setdefault("skill_context_budgets", {})["draft_per_skill_tokens"] = draft_per_skill_tokens
    if repair_per_skill_tokens is not None:
        explicit_overrides["llm"].setdefault("skill_context_budgets", {})["repair_per_skill_tokens"] = repair_per_skill_tokens
    merged = _merge_nested(merged, explicit_overrides)

    llm = merged.get("llm")
    if not isinstance(llm, dict):
        raise RuntimeConfigError("Merged runtime config is missing `llm` settings.")

    provider_value = str(llm.get("provider", "")).strip()
    if provider_value not in SUPPORTED_PROVIDERS:
        raise RuntimeConfigError(
            "Unsupported provider `%s`. Must be one of %s." % (provider_value, sorted(SUPPORTED_PROVIDERS))
        )

    api_key = str(llm.get("api_key", "")).strip()
    base_url_value = str(llm.get("base_url", "")).strip()
    model_value = str(llm.get("model", "")).strip()
    app_name_value = str(llm.get("app_name", "") or "flowevo").strip()

    if provider_value == "openrouter" and not base_url_value:
        base_url_value = OPENROUTER_BASE_URL
    if not api_key:
        api_key = os.getenv("OPENROUTER_API_KEY", "")
    # Hosted OpenRouter always needs a key; self-hosted OpenAI-compatible
    # servers usually ignore it, so only the hosted endpoint is strict.
    if not api_key and is_openrouter_endpoint(base_url_value):
        raise RuntimeConfigError(
            "The openrouter.ai endpoint requires an api_key in the config file "
            "or the OPENROUTER_API_KEY environment variable."
        )

    if not base_url_value:
        raise RuntimeConfigError("Missing runtime base URL.")
    if not model_value:
        raise RuntimeConfigError("Missing runtime model.")

    budgets = llm.get("skill_context_budgets") or {}
    draft_cfg = llm.get("draft") or {}
    repair_cfg = llm.get("repair") or {}
    alfworld_cfg = llm.get("alfworld") or {}
    return RuntimeLLMConfig(
        provider=provider_value,
        api_key=api_key,
        base_url=base_url_value,
        model=model_value,
        app_name=app_name_value,
        skill_top_k=int(llm.get("skill_top_k", 3)),
        skill_context_budgets=SkillContextBudgets(
            draft_total_tokens=int(budgets.get("draft_total_tokens", 1800)),
            repair_total_tokens=int(budgets.get("repair_total_tokens", 1000)),
            draft_per_skill_tokens=int(budgets.get("draft_per_skill_tokens", 550)),
            repair_per_skill_tokens=int(budgets.get("repair_per_skill_tokens", 300)),
        ),
        draft=GenerationSettings(
            temperature=float(draft_cfg.get("temperature", 0.2)),
            max_output_tokens=int(draft_cfg.get("max_output_tokens", 900)),
        ),
        repair=GenerationSettings(
            temperature=float(repair_cfg.get("temperature", 0.15)),
            max_output_tokens=int(repair_cfg.get("max_output_tokens", 900)),
        ),
        config_path=str(config_file),
        local_override_path=str(override_path) if override_path is not None else "",
        grow_on_truncation=bool(llm.get("grow_on_truncation", False)),
        max_output_tokens_cap=int(llm.get("max_output_tokens_cap", 16384) or 16384),
        alfworld=AlfWorldGenerationBudgets(
            step_max_output_tokens=int(alfworld_cfg.get("step_max_output_tokens", 256)),
            compile_max_output_tokens=int(alfworld_cfg.get("compile_max_output_tokens", 500)),
            strategy_max_output_tokens=int(alfworld_cfg.get("strategy_max_output_tokens", 200)),
        ),
    )
