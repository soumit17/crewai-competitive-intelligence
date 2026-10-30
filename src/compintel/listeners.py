"""Event listeners on crewai's event bus (spec §12.3). M0: the cost ledger. M8 adds the live console.

Listeners *observe*; they can't stop a run (that's what hooks are for, see hooks.py). crewai runs sync
handlers on a thread pool, so callers that read the ledger right after a kickoff must first call
`crewai_event_bus.flush()`.
"""

from __future__ import annotations

from typing import Any

from crewai.events import BaseEventListener, LLMCallCompletedEvent
from crewai.events.event_bus import CrewAIEventsBus
from rich.console import Console

from compintel.cost import get_ledger

console = Console(stderr=True)


class CostListener(BaseEventListener):
    """One ledger row per completed LLM call, priced from the provider's usage dict."""

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        # LLMCallCompletedEvent fires once per API response, from inside the provider, so calls made by
        # agents, crews, the planner, memory or guardrails are all caught here.
        @crewai_event_bus.on(LLMCallCompletedEvent)
        def on_llm_completed(source: Any, event: LLMCallCompletedEvent) -> None:
            if not event.usage or not event.model:
                return
            try:
                get_ledger().record(event.model, event.usage, event.response_id, agent=event.agent_role)
            except KeyError as e:
                # The bus swallows handler exceptions; say it loudly instead of losing the cost silently.
                console.print(f"[red]cost ledger: {e}[/red]")


def install() -> CostListener:
    """Register the cost listener on the global bus. Call once per process (tests: inside scoped_handlers)."""
    return CostListener()
