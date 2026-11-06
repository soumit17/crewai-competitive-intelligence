"""Event listeners on crewai's event bus (spec §12.3). M0: cost ledger. M3: guardrail log. M4: delegation log.
M8: RunMonitor (every event family the spec lists, hook dispatches included) and the live console it drives.

Listeners *observe*; they can't stop a run (that's what hooks are for, see hooks.py). crewai runs sync
handlers on a thread pool, so callers that read the ledger right after a kickoff must first call
`crewai_event_bus.flush()`.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from typing import Any

from crewai.events import BaseEventListener, LLMCallCompletedEvent
from crewai.events.event_bus import CrewAIEventsBus
from crewai.events.types.flow_events import (
    HumanFeedbackReceivedEvent,
    HumanFeedbackRequestedEvent,
    MethodExecutionFailedEvent,
    MethodExecutionFinishedEvent,
    MethodExecutionStartedEvent,
)
from crewai.events.types.hook_events import HookDispatchedEvent
from crewai.events.types.llm_events import LLMCallFailedEvent
from crewai.events.types.llm_guardrail_events import LLMGuardrailCompletedEvent
from crewai.events.types.memory_events import (
    MemoryQueryCompletedEvent,
    MemoryQueryFailedEvent,
    MemorySaveCompletedEvent,
    MemorySaveFailedEvent,
)
from crewai.events.types.observation_events import StepObservationFailedEvent
from crewai.events.types.task_events import TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from crewai.events.types.tool_usage_events import (
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

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


def _short(text: Any, n: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _task_label(task: Any) -> str:
    name = getattr(task, "name", None) or getattr(task, "description", None) or "?"
    return _short(name, 40)


class RunMonitor(BaseEventListener):
    """Observes a whole run (spec §12.3): flow methods, tasks, tools, LLM calls, guardrails, memory, human
    feedback and hook dispatches. It keeps counts and a short timeline for the live console and the end-of-run
    summary; it never changes anything (control is hooks.py).

    Handlers run on crewai's thread pool, several at once, so each one runs under the lock (`_on`). Lines for
    the console are printed only after the lock is released: printing while the live panel redraws (which
    reads this monitor under the same lock) would deadlock.
    """

    TIMELINE = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter[str] = Counter()
        self.hooks: Counter[tuple[str, str]] = Counter()  # (interception point, outcome)
        self.hook_aborts: list[str] = []
        self.running_methods: list[str] = []
        self.finished_methods: list[str] = []
        self.active_tasks: dict[int, str] = {}
        self.last_agent: str | None = None
        self.recent_tools: deque[str] = deque(maxlen=4)
        self.timeline: deque[str] = deque(maxlen=self.TIMELINE)
        self.on_line: Any = None  # LiveConsole sets this to print permanent lines above the panel
        self.started = time.perf_counter()
        self._outbox: list[str] = []
        super().__init__()

    def _note(self, line: str) -> None:
        """A line worth keeping (caller holds the lock): into the timeline, and queued for the console."""
        self.timeline.append(line)
        self._outbox.append(line)

    def _on(self, bus: CrewAIEventsBus, event_type: type, handle: Any) -> None:
        """Register `handle(event)` to run under the lock, then print whatever it noted."""

        @bus.on(event_type)
        def handler(source: Any, event: Any) -> None:
            with self._lock:
                handle(event)
                lines, self._outbox = self._outbox, []
            printer = self.on_line
            if printer is not None:
                for line in lines:
                    printer(line)

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        on = lambda event_type: lambda fn: self._on(crewai_event_bus, event_type, fn)  # noqa: E731
        c = self.counts

        # ---- flow methods
        @on(MethodExecutionStartedEvent)
        def method_started(event: MethodExecutionStartedEvent) -> None:
            c["flow_methods_started"] += 1
            self.running_methods.append(event.method_name)

        @on(MethodExecutionFinishedEvent)
        def method_finished(event: MethodExecutionFinishedEvent) -> None:
            c["flow_methods_finished"] += 1
            if event.method_name in self.running_methods:
                self.running_methods.remove(event.method_name)
            self.finished_methods.append(event.method_name)
            self._note(f"[green]✓ step[/green] {event.method_name}")

        @on(MethodExecutionFailedEvent)
        def method_failed(event: MethodExecutionFailedEvent) -> None:
            c["flow_methods_failed"] += 1
            if event.method_name in self.running_methods:
                self.running_methods.remove(event.method_name)
            self._note(f"[red]✗ step {event.method_name}[/red]: {_short(event.error, 120)}")

        # ---- tasks
        @on(TaskStartedEvent)
        def task_started(event: TaskStartedEvent) -> None:
            c["tasks_started"] += 1
            self.active_tasks[id(event.task)] = _task_label(event.task)

        @on(TaskCompletedEvent)
        def task_completed(event: TaskCompletedEvent) -> None:
            c["tasks_completed"] += 1
            label = self.active_tasks.pop(id(event.task), None) or _task_label(event.task)
            agent = getattr(event.output, "agent", None) or "?"
            self._note(f"  [green]✓ task[/green] {label} [dim]({agent})[/dim]")

        @on(TaskFailedEvent)
        def task_failed(event: TaskFailedEvent) -> None:
            c["tasks_failed"] += 1
            label = self.active_tasks.pop(id(event.task), None) or _task_label(event.task)
            self._note(f"  [red]✗ task {label}[/red]: {_short(event.error, 120)}")

        # ---- tools
        @on(ToolUsageStartedEvent)
        def tool_started(event: ToolUsageStartedEvent) -> None:
            c["tool_calls"] += 1
            self.last_agent = event.agent_role or self.last_agent
            self.recent_tools.append(f"{event.tool_name} [dim]({_short(event.agent_role, 30)})[/dim]")

        @on(ToolUsageFinishedEvent)
        def tool_finished(event: ToolUsageFinishedEvent) -> None:
            c["tools_finished"] += 1
            if event.from_cache:
                c["tool_cache_hits"] += 1

        @on(ToolUsageErrorEvent)
        def tool_error(event: ToolUsageErrorEvent) -> None:
            c["tool_errors"] += 1
            self._note(f"    [red]tool error[/red] {event.tool_name}: {_short(event.error, 100)}")

        # ---- LLM calls (pricing them is CostListener's job)
        @on(LLMCallCompletedEvent)
        def llm_completed(event: LLMCallCompletedEvent) -> None:
            c["llm_calls"] += 1
            self.last_agent = event.agent_role or self.last_agent

        # ---- guardrails
        @on(LLMGuardrailCompletedEvent)
        def guardrail(event: LLMGuardrailCompletedEvent) -> None:
            c["guardrails_passed" if event.success else "guardrails_failed"] += 1
            if not event.success:
                self._note(
                    f"    [yellow]guardrail retry[/yellow] {_short(event.guardrail_name, 40)} "
                    f"(attempt {event.retry_count + 1}): {_short(event.error, 90)}"
                )

        # ---- memory
        @on(MemorySaveCompletedEvent)
        def memory_saved(event: MemorySaveCompletedEvent) -> None:
            c["memory_saves"] += 1

        @on(MemoryQueryCompletedEvent)
        def memory_query(event: MemoryQueryCompletedEvent) -> None:
            c["memory_queries"] += 1

        @on(MemorySaveFailedEvent)
        def memory_save_failed(event: MemorySaveFailedEvent) -> None:
            c["memory_failures"] += 1
            self._note(f"    [red]memory save failed[/red]: {_short(event.error, 100)}")

        @on(MemoryQueryFailedEvent)
        def memory_query_failed(event: MemoryQueryFailedEvent) -> None:
            c["memory_failures"] += 1
            self._note(f"    [red]memory recall failed[/red]: {_short(event.error, 100)}")

        # ---- human feedback
        @on(HumanFeedbackRequestedEvent)
        def feedback_requested(event: HumanFeedbackRequestedEvent) -> None:
            c["feedback_requested"] += 1
            self._note(f"[magenta]review requested[/magenta] at {event.method_name}")

        @on(HumanFeedbackReceivedEvent)
        def feedback_received(event: HumanFeedbackReceivedEvent) -> None:
            c["feedback_received"] += 1
            # ConsoleProvider emits this before the answer is mapped to an outcome: outcome is usually None.
            outcome = f" → {event.outcome}" if event.outcome else ""
            self._note(f"[magenta]review answered{outcome}[/magenta] ← {_short(event.feedback, 60)!r}")

        # ---- hooks: one event per dispatch (per interception point that had hooks), not per hook
        @on(HookDispatchedEvent)
        def hook_dispatched(event: HookDispatchedEvent) -> None:
            self.hooks[(event.interception_point, event.outcome)] += 1
            if event.outcome == "aborted":
                line = f"{event.abort_source}: {event.abort_reason}"
                self.hook_aborts.append(line)
                self._note(f"    [red]hook abort[/red] {_short(line, 140)}")
            elif event.outcome == "modified" and event.interception_point == "post_model_call":
                self._note("    [yellow]hook modified a model response[/yellow] (secret redacted)")

    # ---- summaries
    def hook_summary(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for (_, outcome), n in self.hooks.items():
            out[outcome] += n
        out["dispatched"] = sum(self.hooks.values())
        return dict(out)

    def render(self) -> Panel:
        """The live panel: what's running now and the running totals."""
        from compintel.cost import get_ledger

        with self._lock:
            c = self.counts
            hs = self.hook_summary()
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold cyan", no_wrap=True)
            table.add_column()
            table.add_row(
                "flow step",
                ", ".join(self.running_methods) or "-",
            )
            table.add_row("tasks", ", ".join(self.active_tasks.values()) or "-")
            table.add_row("agent", _short(self.last_agent, 70) or "-")
            table.add_row(
                "tools",
                f"{c['tool_calls']} calls, {c['tool_cache_hits']} cached, {c['tool_errors']} errors"
                + (f" · last: {self.recent_tools[-1]}" if self.recent_tools else ""),
            )
            table.add_row(
                "guardrails", f"{c['guardrails_passed']} passed, {c['guardrails_failed']} sent back"
            )
            table.add_row("memory", f"{c['memory_saves']} saved, {c['memory_queries']} recalls")
            table.add_row(
                "hooks",
                f"{hs.get('dispatched', 0)} dispatches, {hs.get('modified', 0)} modified, "
                f"{hs.get('aborted', 0)} aborted",
            )
        ledger = get_ledger()
        table.add_row(
            "LLM",
            f"{c['llm_calls']} calls · P3 ${ledger.spent_usd:.4f} of ${ledger.cap_usd:.2f} · "
            f"{time.perf_counter() - self.started:.0f}s",
        )
        return Panel(Group(table), title="compintel", border_style="cyan", expand=False)


class LiveConsole:
    """A rich Live panel fed by a RunMonitor, plus permanent lines above it (finished steps, guardrail
    retries, hook aborts). On stderr, so stdout keeps only the brief and the summary.

    `paused()` takes the panel down while someone types: the console review uses it around input().
    """

    def __init__(self, monitor: RunMonitor, console: Console | None = None) -> None:
        self.monitor = monitor
        self.console = console or Console(stderr=True)
        self._live: Live | None = None

    def __enter__(self) -> LiveConsole:
        global _active_live
        self._live = Live(
            console=self.console,
            get_renderable=self.monitor.render,
            refresh_per_second=4,
            transient=True,  # the panel disappears at the end; the summary is printed after
        )
        self._live.start()
        self.monitor.on_line = self.console.print
        _active_live = self
        return self

    def __exit__(self, *exc: Any) -> None:
        global _active_live
        self.monitor.on_line = None
        if self._live is not None:
            self._live.stop()
        _active_live = None

    @contextmanager
    def paused(self):
        live = self._live
        if live is None or not live.is_started:
            yield
            return
        live.stop()
        try:
            yield
        finally:
            live.start()


_active_live: LiveConsole | None = None
_monitor: RunMonitor | None = None


@contextmanager
def live_paused():
    """Pause the live console, if one is showing (no-op otherwise). For code that reads from the terminal."""
    if _active_live is None:
        yield
    else:
        with _active_live.paused():
            yield


def monitor() -> RunMonitor | None:
    """This process's RunMonitor (installed by `install()`), for end-of-run summaries."""
    return _monitor


def install() -> CostListener:
    """Register the cost listener and the run monitor on the global bus. Call once per process (tests: inside
    scoped_handlers)."""
    global _monitor
    _monitor = RunMonitor()
    return CostListener()
