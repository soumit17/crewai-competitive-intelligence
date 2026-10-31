"""`@on` execution hooks (spec §12.1). M0: budget gate. M2: URL allowlist. M3: file allowlist. M8: the rest.

Hooks *control* (listeners only observe). Only `HookAborted` stops a run: any other exception raised in
a hook is swallowed (fail-open), so a buggy hook can't crash a run but also can't protect it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted, InterceptionPoint, on
from crewai.hooks.dispatch import get_hooks

from compintel.config import get_settings
from compintel.cost import get_ledger

# crewai sanitizes tool names before matching `tools=` filters: "Read website content" -> this.
SCRAPE_TOOL = "read_website_content"
FILE_READ_TOOL = "read_a_files_content"  # crewai-tools' FileReadTool ("Read a file's content")
READABLE_DIR = Path("briefs")  # cwd-relative, like the briefs the crews write


def budget_gate(ctx) -> None:
    """Refuse every model call once the project has spent its cap.

    PRE_MODEL_CALL rather than the spec's EXECUTION_START: it also covers `Agent.kickoff` (no crew/flow
    execution boundary) and stops a run that crosses the cap midway, not only at its start.
    """
    # The ledger is fed by a listener on a thread pool; wait for in-flight rows so the total is current.
    crewai_event_bus.flush()
    ledger = get_ledger()
    if ledger.over_cap:
        raise HookAborted(
            reason=f"P3 spend cap reached: ${ledger.spent_usd:.4f} of ${ledger.cap_usd:.2f}",
            source="budget-gate",
        )


def url_allowlist(ctx) -> None:
    """The network policy for LLM-chosen URLs: only the fake web server.

    crewai-tools' own SSRF guard blocks 127.0.0.1, so compintel runs switch it off
    (CREWAI_TOOLS_ALLOW_UNSAFE_PATHS, see config.apply_env). This hook takes its place, and is stricter:
    one host:port instead of "any public address".
    """
    allowed = urlparse(get_settings().fakeweb_url)
    raw = str((ctx.tool_input or {}).get("website_url") or "")
    url = urlparse(raw)
    ok = (
        url.scheme in ("http", "https")
        and url.username is None  # "http://127.0.0.1:8765@evil.example" has hostname evil.example
        and (url.hostname, url.port) == (allowed.hostname, allowed.port)
    )
    if not ok:
        raise HookAborted(reason=f"URL not allowed: {raw!r} (only {allowed.netloc})", source="url-allowlist")


def file_read_allowlist(ctx) -> None:
    """The file policy for LLM-chosen paths: only files under briefs/.

    Same story as url_allowlist: the escape hatch that lets the scraper reach 127.0.0.1 also lifts
    FileReadTool's own base_dir containment, so this hook takes its place. Paths are resolved first, so
    "briefs/../.env" and symlinks out of briefs/ are caught.
    """
    raw = str((ctx.tool_input or {}).get("file_path") or "")
    allowed = READABLE_DIR.resolve()
    target = Path(raw).resolve() if raw else None
    if target is None or not target.is_relative_to(allowed):
        raise HookAborted(
            reason=f"File not allowed: {raw!r} (only under {READABLE_DIR}/)", source="file-allowlist"
        )


def is_installed(hook) -> bool:
    """True if `hook` is registered globally (`@on` with filters registers `hook._registered_hook`)."""
    registered = [h for point in InterceptionPoint for h in get_hooks(point)]
    return hook in registered or getattr(hook, "_registered_hook", None) in registered


def install() -> None:
    """Register the global hooks. Tests clear them with `clear_all_hooks()` after each test."""
    on(InterceptionPoint.PRE_MODEL_CALL)(budget_gate)
    # tools= scopes the hook: it only runs for the scrape tool, so other tool calls skip it entirely.
    on(InterceptionPoint.PRE_TOOL_CALL, tools=[SCRAPE_TOOL])(url_allowlist)
    on(InterceptionPoint.PRE_TOOL_CALL, tools=[FILE_READ_TOOL])(file_read_allowlist)
