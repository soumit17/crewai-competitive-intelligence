"""$0 test guards (spec §16.1): no network, no real model, no OpenAI key, no writes to real run data.

Network is blocked by pytest-socket except 127.0.0.1 (see `addopts` in pyproject.toml).
"""

from __future__ import annotations

import os
import tempfile

# Set before any crewai / compintel import: crewai reads these on import, Settings on first use.
os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy"
os.environ.pop("OPENAI_API_KEY", None)  # spec Rule 3: everything must work without it
os.environ["MODEL"] = "anthropic/claude-haiku-5-5"
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_DISABLE_VERSION_CHECK"] = "true"
os.environ["CREWAI_TOOLS_ALLOW_UNSAFE_PATHS"] = (
    "true"  # as config.apply_env() does; url_allowlist polices URLs
)
os.environ.pop("P3_RUNS_DIR", None)
# crewai's chroma store path is a module constant computed on first import (rag/chromadb/constants.py), from
# CREWAI_STORAGE_DIR (an app *name*, joined under XDG_DATA_HOME) or else the cwd's name: set both before
# anything imports crewai, or tests write into the developer's real ~/.local/share/<project> store.
_TEST_DATA_HOME = tempfile.mkdtemp(prefix="compintel-tests-")
os.environ["XDG_DATA_HOME"] = _TEST_DATA_HOME
os.environ["CREWAI_STORAGE_DIR"] = "compintel-tests"

import pytest  # noqa: E402
from crewai.events.event_bus import crewai_event_bus  # noqa: E402
from crewai.hooks import clear_all_hooks  # noqa: E402

from compintel.config import get_settings  # noqa: E402
from compintel.cost import get_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Crews write cwd-relative briefs/ (crewai's output_file can't be made absolute from inputs): run in tmp.
    monkeypatch.chdir(tmp_path)
    from compintel import guardrails

    guardrails.reset()
    monkeypatch.setenv("P3_RUNS_DIR", str(tmp_path / "runs"))
    get_settings.cache_clear()
    get_ledger.cache_clear()
    # Global hooks and bus handlers outlive a test unless cleared (spec §2.2 gotcha).
    clear_all_hooks()
    from compintel.config import embedder_scope
    from tests.fakes import FAKE_EMBEDDER_SPEC

    # Every test embeds with the hash fake: the real ONNX embedder would download a model (network blocked).
    with crewai_event_bus.scoped_handlers(), embedder_scope(FAKE_EMBEDDER_SPEC):
        yield
        crewai_event_bus.flush()
    clear_all_hooks()
    get_settings.cache_clear()
    get_ledger.cache_clear()


@pytest.fixture(scope="session")
def world_dir(tmp_path_factory):
    """A freshly generated mock world (data/ + knowledge/), shared by the whole test session."""
    from compintel.world.generate import generate

    root = tmp_path_factory.mktemp("world")
    generate(root)
    return root


@pytest.fixture(scope="session")
def _fakeweb_server(world_dir):
    import threading

    from compintel.world.fakeweb import make_server

    server = make_server(world_dir / "data" / "fakeweb", port=0)  # any free port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def world(world_dir, _fakeweb_server, monkeypatch):
    """Point tools, MCP server and allowlist at the test world and its fake web server."""
    monkeypatch.setenv("P3_DATA_DIR", str(world_dir / "data"))
    monkeypatch.setenv("P3_KNOWLEDGE_DIR", str(world_dir / "knowledge"))
    monkeypatch.setenv("FAKEWEB_URL", _fakeweb_server)
    get_settings.cache_clear()
    return _fakeweb_server
