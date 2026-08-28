from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.config import RuntimeConfigError, load_runtime_config  # noqa: E402


def write_cfg(tmp_path: Path, body: str) -> str:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_local_openai_compatible_server_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = write_cfg(tmp_path, "llm:\n  provider: openai_compatible\n  base_url: http://localhost:8000/v1\n  model: local-model\n")
    cfg = load_runtime_config(config_path=path)
    assert cfg.provider == "openai_compatible" and cfg.api_key == "" and cfg.model == "local-model"
    assert cfg.grow_on_truncation is False and cfg.max_output_tokens_cap == 16384


def test_openrouter_alias_with_custom_base_url_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = write_cfg(tmp_path, "llm:\n  provider: openrouter\n  base_url: http://10.0.0.5:8000/v1\n  model: m\n")
    assert load_runtime_config(config_path=path).base_url == "http://10.0.0.5:8000/v1"


def test_hosted_openrouter_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = write_cfg(tmp_path, "llm:\n  provider: openrouter\n  model: openai/gpt-4o-mini\n")
    with pytest.raises(RuntimeConfigError, match="openrouter.ai"):
        load_runtime_config(config_path=path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-x")
    cfg = load_runtime_config(config_path=path)
    assert cfg.base_url == "https://openrouter.ai/api/v1" and cfg.api_key == "sk-x"


def test_grow_on_truncation_parsed(tmp_path):
    path = write_cfg(
        tmp_path,
        "llm:\n  provider: openai_compatible\n  base_url: http://h/v1\n  model: m\n  grow_on_truncation: true\n  max_output_tokens_cap: 8192\n",
    )
    cfg = load_runtime_config(config_path=path)
    assert cfg.grow_on_truncation is True and cfg.max_output_tokens_cap == 8192


def test_alfworld_budgets_default_and_override(tmp_path):
    from runtime.config import AlfWorldGenerationBudgets, alfworld_budgets

    path = write_cfg(tmp_path, "llm:\n  provider: openai_compatible\n  base_url: http://h/v1\n  model: m\n")
    cfg = load_runtime_config(config_path=path)
    assert cfg.alfworld == AlfWorldGenerationBudgets(256, 500, 200)

    path2 = write_cfg(
        tmp_path / "b",
        "llm:\n  provider: openai_compatible\n  base_url: http://h/v1\n  model: m\n  alfworld:\n    step_max_output_tokens: 2048\n",
    )
    cfg2 = load_runtime_config(config_path=path2)
    assert cfg2.alfworld.step_max_output_tokens == 2048 and cfg2.alfworld.compile_max_output_tokens == 500

    class Client:
        config = cfg2

    assert alfworld_budgets(Client()).step_max_output_tokens == 2048
    assert alfworld_budgets(object()).step_max_output_tokens == 256


def test_unknown_provider_rejected(tmp_path):
    path = write_cfg(tmp_path, "llm:\n  provider: anthropic\n  base_url: http://h\n  model: m\n")
    with pytest.raises(RuntimeConfigError, match="Unsupported provider"):
        load_runtime_config(config_path=path)
