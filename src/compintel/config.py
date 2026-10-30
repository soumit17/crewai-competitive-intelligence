"""Process-wide settings and the single place every LLM / embedder / limit is built (spec §19).

Rule 3 of the spec: CrewAI silently defaults to OpenAI in several places (docs/decisions.md lists them).
Everything that needs a model asks this module, so no component is left on a default.
This module never imports crewai at import time: `apply_env()` must run before crewai reads its env vars.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from crewai import LLM

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    model_dev: str = "anthropic/claude-haiku-5-5"
    model_final: str = "anthropic/claude-sonnet-5-5"
    p3_spend_cap_usd: float = 12.0
    max_tokens: int = 1500
    embedder_provider: str = "onnx"
    embedder_model: str = "all-MiniLM-L6-v2"
    fakeweb_url: str = "http://127.0.0.1:8765"
    # Tests point these at tmp dirs: the real ledger never sees fake data; tests read a fresh world.
    p3_runs_dir: Path = PROJECT_ROOT / "runs"
    p3_data_dir: Path = PROJECT_ROOT / "data"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Applied with setdefault, so a value already in the shell (or set by a test) wins.
_ENV_DEFAULTS = {
    # create_llm() reads MODEL before falling back to gpt-4.1-mini: a net for any component we forget.
    "MODEL": None,  # filled from settings.model_dev
    "CREWAI_DISABLE_TELEMETRY": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_DISABLE_VERSION_CHECK": "true",
}


def apply_env() -> None:
    """Export the env vars crewai reads itself. Call before the first crewai import."""
    s = get_settings()
    for key, value in _ENV_DEFAULTS.items():
        os.environ.setdefault(key, value if value is not None else s.model_dev)
    # crewai-tools' SSRF guard blocks 127.0.0.1, i.e. our fake web server. Its only switch is this
    # process-wide escape hatch (it also lifts file-path containment). Set inside compintel runs only,
    # never in the shell; hooks.url_allowlist is the network policy instead, and scouts refuse to start
    # without it.
    os.environ["CREWAI_TOOLS_ALLOW_UNSAFE_PATHS"] = "true"
    # The native Anthropic provider reads the key from the environment when none is passed.
    if s.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", s.anthropic_api_key)


def model_id(final: bool = False) -> str:
    s = get_settings()
    return s.model_final if final else s.model_dev


def make_llm(final: bool = False, max_tokens: int | None = None) -> LLM:
    """The only way this project builds an LLM.

    `anthropic/` prefix → crewai's native AnthropicCompletion (no LiteLLM). No temperature: Claude 5.5 models
    reject non-default sampling values, and the provider only sends it when set.
    """
    from crewai import LLM

    s = get_settings()
    return LLM(model=model_id(final), max_tokens=max_tokens or s.max_tokens)


# Every agent gets explicit limits (spec Rule 8): CrewAI's ReAct loop is the main way spend runs away.
AGENT_LIMITS: dict[str, Any] = {"max_iter": 8, "max_rpm": 20, "max_execution_time": 180}


def embedder_config() -> dict[str, Any]:
    """Local embedder for Memory and Knowledge (replaces crewai's OpenAI default, decisions.md #4/#5).

    `onnx` = all-MiniLM-L6-v2 run through chromadb's bundled ONNX runtime: no extra dependency, no API key.
    First use downloads ~80 MB to ~/.cache/chroma. Used from M4 on.
    """
    s = get_settings()
    if s.embedder_provider == "onnx":
        return {"provider": "onnx", "config": {}}
    return {"provider": s.embedder_provider, "config": {"model_name": s.embedder_model}}
