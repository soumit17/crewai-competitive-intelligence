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


def restore_std_streams() -> None:
    """Put the real stdout/stderr back if a run left them on a StringIO.

    crewai's chromadb client silences queries with `contextlib.redirect_stdout(io.StringIO())`, which swaps
    the process-wide sys.stdout. The deep dive's parallel analysts query knowledge at the same time, two
    redirects interleave, and the last one to exit "restores" the other's StringIO: everything printed after
    the run vanished, exit code 0 (live week 2, M6 and M7). Not thread-safe upstream; repaired here.
    """
    import io
    import sys

    if isinstance(sys.stdout, io.StringIO):
        sys.stdout = sys.__stdout__
    if isinstance(sys.stderr, io.StringIO):
        sys.stderr = sys.__stderr__
