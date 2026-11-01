"""Event listeners on crewai's event bus (spec §12.3). M0: cost ledger. M3: guardrail log. M4: delegation log.
M8: live console.

Listeners *observe*; they can't stop a run (that's what hooks are for, see hooks.py). crewai runs sync
handlers on a thread pool, so callers that read the ledger right after a kickoff must first call
`crewai_event_bus.flush()`.
"""

from __future__ import annotations

import json
from typing import Any

from crewai.events import BaseEventListener, LLMCallCompletedEvent
from crewai.events.event_bus import CrewAIEventsBus
from crewai.events.types.llm_events import LLMCallFailedEvent
from crewai.events.types.llm_guardrail_events import LLMGuardrailCompletedEvent
from crewai.events.types.observation_events import StepObservationFailedEvent
from crewai.events.types.tool_usage_events import ToolUsageStartedEvent
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


class GuardrailLog(BaseEventListener):
    """Every guardrail verdict of a run, function and LLM alike (crewai emits the same event for both)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        super().__init__()

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        @crewai_event_bus.on(LLMGuardrailCompletedEvent)
        def on_guardrail(source: Any, event: LLMGuardrailCompletedEvent) -> None:
            self.entries.append(
                {
                    "type": event.guardrail_type,  # "function" or "llm"
                    "name": event.guardrail_name,
                    "attempt": event.retry_count + 1,
                    "passed": event.success,
                    "error": event.error,
                }
            )


DELEGATION_TOOLS = {"delegate_work_to_coworker": "delegate", "ask_question_to_coworker": "ask"}


class DelegationLog(BaseEventListener):
    """Every delegation a manager makes: crewai's delegation tools are ordinary tool calls on the bus."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        super().__init__()

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        @crewai_event_bus.on(ToolUsageStartedEvent)
        def on_tool(source: Any, event: ToolUsageStartedEvent) -> None:
            kind = DELEGATION_TOOLS.get(event.tool_name.lower().replace(" ", "_"))
            if kind is None:
                return
            args = event.tool_args
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            self.entries.append(
                {
                    "kind": kind,
                    "by": event.agent_role,
                    "coworker": args.get("coworker"),
                    "task": event.task_name,
                }
            )


class FailureLog(BaseEventListener):
    """Failures crewai recovers from by itself, so a run still "succeeds": failed LLM calls (e.g. a structured
    reply that didn't validate) and failed agent-planning step observations (crewai then assumes the step
    succeeded). Counted so they can't hide in a log stream again (they did in the first M4 comparison)."""

    def __init__(self) -> None:
        self.llm_call_failures: list[str] = []
        self.observation_failures: list[str] = []
        super().__init__()

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        @crewai_event_bus.on(LLMCallFailedEvent)
        def on_llm_failed(source: Any, event: LLMCallFailedEvent) -> None:
            self.llm_call_failures.append(f"{event.agent_role}: {event.error[:200]}")

        @crewai_event_bus.on(StepObservationFailedEvent)
        def on_observation_failed(source: Any, event: StepObservationFailedEvent) -> None:
            self.observation_failures.append(f"{event.agent_role}: {event.error[:200]}")


def install() -> CostListener:
    """Register the cost listener on the global bus. Call once per process (tests: inside scoped_handlers)."""
    return CostListener()
