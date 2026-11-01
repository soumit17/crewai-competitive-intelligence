"""Process bootstrap shared by every entry point (the `compintel` CLI and `main.py` for `crewai run`)."""

from __future__ import annotations

_done = False


def bootstrap() -> None:
    """Env first (crewai reads telemetry/MODEL vars on import), then the global cost listener and hooks.
    Idempotent."""
    global _done
    if _done:
        return
    from compintel.config import apply_env

    apply_env()
    from compintel import hooks, listeners

    listeners.install()
    hooks.install()
    _done = True
