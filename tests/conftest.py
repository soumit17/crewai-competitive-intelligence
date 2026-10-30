"""$0 test guards (spec §16.1): no network, no real model, no OpenAI key, no writes to real run data.

Network is blocked by pytest-socket except 127.0.0.1 (see `addopts` in pyproject.toml).
"""

from __future__ import annotations

import os

# Set before any crewai / compintel import: crewai reads these on import, Settings on first use.
os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy"
os.environ.pop("OPENAI_API_KEY", None)  # spec Rule 3: everything must work without it
os.environ["MODEL"] = "anthropic/claude-haiku-5-5"
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_DISABLE_VERSION_CHECK"] = "true"
os.environ.pop("P3_RUNS_DIR", None)

import pytest  # noqa: E402
from crewai.events.event_bus import crewai_event_bus  # noqa: E402
from crewai.hooks import clear_all_hooks  # noqa: E402

from compintel.config import get_settings  # noqa: E402
from compintel.cost import get_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("P3_RUNS_DIR", str(tmp_path / "runs"))
    # crewai's own local stores (memory, knowledge, flow persistence default dir) go to tmp too.
    monkeypatch.setenv("CREWAI_STORAGE_DIR", str(tmp_path / "crewai_storage"))
    get_settings.cache_clear()
    get_ledger.cache_clear()
    # Global hooks and bus handlers outlive a test unless cleared (spec §2.2 gotcha).
    clear_all_hooks()
    with crewai_event_bus.scoped_handlers():
        yield
        crewai_event_bus.flush()
    clear_all_hooks()
    get_settings.cache_clear()
    get_ledger.cache_clear()
