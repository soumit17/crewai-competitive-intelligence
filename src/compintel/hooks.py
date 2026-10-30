"""`@on` execution hooks (spec §12.1). M0: the budget gate. M2 adds the URL allowlist, M8 the rest.

Hooks *control* (listeners only observe). Only `HookAborted` stops a run: any other exception raised in
a hook is swallowed (fail-open), so a buggy hook can't crash a run but also can't protect it.
"""

from __future__ import annotations

from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted, InterceptionPoint, on

from compintel.cost import get_ledger


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


def install() -> None:
    """Register the global hooks. Tests clear them with `clear_all_hooks()` after each test."""
    on(InterceptionPoint.PRE_MODEL_CALL)(budget_gate)
