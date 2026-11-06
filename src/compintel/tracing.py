"""Tracing to a local Arize Phoenix (spec §15, M8b): one OpenTelemetry trace per run, each crewai step a span.

How: `openinference-instrumentation-crewai` in its event-listener mode (`OpenInferenceEventListener`). It
turns crewai's own event bus into spans (flow → method → crew → task → agent → LLM call / tool), nested by
the bus's `parent_event_id`: no monkey-patching of crewai methods, unlike its default wrapper mode.

`CompintelSpans` adds what that listener leaves out, using the same span assembler so they land in the same
tree:
- every other crewai scope our runs open: guardrails, memory save/query/retrieval, knowledge queries, MCP
  connections and MCP tool calls, agent reasoning (planning). Not cosmetic: the assembler parks a child span
  until its parent's span exists, so without these the LLM calls *inside* a guardrail or a memory save
  would never be exported at all;
- hook dispatches that changed something (aborted / modified), as zero-length spans; "proceeded" ones are
  left out (there are several per LLM call);
- the human review, from request to answer (how long the run waited on a person);
- the agent role and task on every LLM span, so tokens can be grouped per agent (`scripts/trace_report.py`).

The Phoenix server is not in this venv (dependency clash, see pyproject.toml); run it beside:
    uvx --from arize-phoenix==21.0.0 phoenix serve      → http://127.0.0.1:6006

The private `_event_assembler` / `_event_listener` modules are pinned with the package (1.1.23);
tests/test_tracing.py pins the behaviour this module relies on.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from crewai.events.types.flow_events import HumanFeedbackReceivedEvent, HumanFeedbackRequestedEvent
from crewai.events.types.hook_events import HookDispatchedEvent
from crewai.events.types.knowledge_events import (
    KnowledgeQueryCompletedEvent,
    KnowledgeQueryFailedEvent,
    KnowledgeQueryStartedEvent,
    KnowledgeRetrievalCompletedEvent,
    KnowledgeRetrievalStartedEvent,
    KnowledgeSearchQueryFailedEvent,
)
from crewai.events.types.llm_events import LLMCallStartedEvent
from crewai.events.types.llm_guardrail_events import LLMGuardrailCompletedEvent, LLMGuardrailStartedEvent
from crewai.events.types.mcp_events import (
    MCPConnectionCompletedEvent,
    MCPConnectionFailedEvent,
    MCPConnectionStartedEvent,
    MCPToolExecutionCompletedEvent,
    MCPToolExecutionFailedEvent,
    MCPToolExecutionStartedEvent,
)
from crewai.events.types.memory_events import (
    MemoryQueryCompletedEvent,
    MemoryQueryFailedEvent,
    MemoryQueryStartedEvent,
    MemoryRetrievalCompletedEvent,
    MemoryRetrievalFailedEvent,
    MemoryRetrievalStartedEvent,
    MemorySaveCompletedEvent,
    MemorySaveFailedEvent,
    MemorySaveStartedEvent,
)
from crewai.events.types.reasoning_events import (
    AgentReasoningCompletedEvent,
    AgentReasoningFailedEvent,
    AgentReasoningStartedEvent,
)
from openinference.instrumentation.crewai._event_assembler import _SpanEndSpec, _SpanStartSpec
from openinference.instrumentation.crewai._event_listener import (
    OpenInferenceEventListener,
    _build_llm_start_spec,
)
from openinference.semconv.trace import OpenInferenceSpanKindValues as Kind
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

PHOENIX_URL = "http://127.0.0.1:6006"
PROJECT = "compintel"
PHOENIX_COMMAND = "uvx --from arize-phoenix==21.0.0 phoenix serve"

# Span attributes this project adds (read back by scripts/trace_report.py).
AGENT_ATTR = "compintel.agent"
TASK_ATTR = "compintel.task"


def _short(value: Any, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def _no_output(_: Any) -> None:
    return None


def _no_attrs(_: Any) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class _Scope:
    """A crewai start/end event pair → one span."""

    start: type
    ends: tuple[type, ...]
    fails: tuple[type, ...]
    kind: Kind
    name: Callable[[Any], str]
    input: Callable[[Any], Any] = _no_output  # from the start event
    output: Callable[[Any], Any] = _no_output  # from a successful end event
    attrs: Callable[[Any], dict[str, Any]] = _no_attrs  # from any end event
    end_error: Callable[[Any], str | None] = _no_output  # an "end" event that still means failure


def _guardrail_error(e: LLMGuardrailCompletedEvent) -> str | None:
    """A failed guardrail is an error span (red in Phoenix): the task goes back to the agent for a retry."""
    return None if e.success else f"guardrail failed (attempt {e.retry_count + 1}): {e.error or e.result}"


_SCOPES: tuple[_Scope, ...] = (
    _Scope(
        LLMGuardrailStartedEvent,
        (LLMGuardrailCompletedEvent,),
        (),
        Kind.GUARDRAIL,
        name=lambda e: f"guardrail {e.guardrail_name or e.guardrail_type or ''}".strip(),
        input=lambda e: e.task_name,
        output=lambda e: _short(e.result),
        attrs=lambda e: {"guardrail.success": e.success, "guardrail.retry_count": e.retry_count},
        end_error=_guardrail_error,
    ),
    _Scope(
        MemorySaveStartedEvent,
        (MemorySaveCompletedEvent,),
        (MemorySaveFailedEvent,),
        Kind.CHAIN,
        name=lambda e: "memory.save",
        input=lambda e: _short(e.value or ""),
        output=lambda e: f"saved in {e.save_time_ms:.0f} ms",
        attrs=lambda e: {"memory.save_ms": getattr(e, "save_time_ms", 0.0)},
    ),
    _Scope(
        MemoryQueryStartedEvent,
        (MemoryQueryCompletedEvent,),
        (MemoryQueryFailedEvent,),
        Kind.RETRIEVER,
        name=lambda e: "memory.recall",
        input=lambda e: e.query,
        output=lambda e: _short(e.results),
        attrs=lambda e: {
            "memory.results": len(e.results) if isinstance(getattr(e, "results", None), list) else -1,
            "memory.query_ms": getattr(e, "query_time_ms", 0.0),
        },
    ),
    _Scope(
        MemoryRetrievalStartedEvent,
        (MemoryRetrievalCompletedEvent,),
        (MemoryRetrievalFailedEvent,),
        Kind.RETRIEVER,
        name=lambda e: "memory.retrieve_for_task",
        input=lambda e: e.task_name,
        output=lambda e: _short(e.memory_content),
    ),
    _Scope(
        KnowledgeQueryStartedEvent,
        (KnowledgeQueryCompletedEvent,),
        (KnowledgeQueryFailedEvent,),
        Kind.CHAIN,
        name=lambda e: "knowledge.query",
        input=lambda e: _short(e.task_prompt, 500),
        output=lambda e: e.query,
    ),
    _Scope(
        KnowledgeRetrievalStartedEvent,
        (KnowledgeRetrievalCompletedEvent,),
        (KnowledgeSearchQueryFailedEvent,),
        Kind.RETRIEVER,
        name=lambda e: "knowledge.search",
        output=lambda e: _short(e.retrieved_knowledge),
        attrs=lambda e: {"knowledge.query": getattr(e, "query", "")},
    ),
    _Scope(
        MCPConnectionStartedEvent,
        (MCPConnectionCompletedEvent,),
        (MCPConnectionFailedEvent,),
        Kind.CHAIN,
        name=lambda e: f"mcp.connect {e.server_name}",
        input=lambda e: e.transport_type,
    ),
    _Scope(
        MCPToolExecutionStartedEvent,
        (MCPToolExecutionCompletedEvent,),
        (MCPToolExecutionFailedEvent,),
        Kind.TOOL,
        name=lambda e: f"mcp.tool {e.tool_name}",
        input=lambda e: e.tool_args,
        output=lambda e: _short(e.result),
    ),
    _Scope(
        AgentReasoningStartedEvent,
        (AgentReasoningCompletedEvent,),
        (AgentReasoningFailedEvent,),
        Kind.CHAIN,
        name=lambda e: f"reasoning {e.agent_role}",
        input=lambda e: e.task_name,
        output=lambda e: _short(e.plan),
        attrs=lambda e: {"reasoning.ready": getattr(e, "ready", False)},
    ),
)


class CompintelSpans(OpenInferenceEventListener):
    """OpenInference's crewai event listener plus the spans listed in the module docstring."""

    def __init__(self, **kwargs: Any) -> None:
        self._reviews: dict[tuple[str, str], str] = {}  # (flow_name, method) → requested event id
        self._reviews_lock = threading.Lock()
        super().__init__(**kwargs)

    def setup_listeners(self, crewai_event_bus: Any) -> None:
        super().setup_listeners(crewai_event_bus)
        for scope in _SCOPES:
            self._register(scope.start, self._starter(scope))
            for end in scope.ends:
                self._register(end, self._ender(scope, failed=False))
            for fail in scope.fails:
                self._register(fail, self._ender(scope, failed=True))
        self._register(HookDispatchedEvent, self._on_hook)
        self._register(HumanFeedbackRequestedEvent, self._on_review_requested)
        self._register(HumanFeedbackReceivedEvent, self._on_review_received)

    # Replaces the parent's handler (registered under this name): same span, plus who made the call.
    def _on_llm_started(self, source: Any, event: LLMCallStartedEvent) -> None:
        if self._is_suppressed():
            return
        spec = _build_llm_start_spec(event)
        extra = {AGENT_ATTR: getattr(event, "agent_role", None) or "-"}
        if task := getattr(event, "task_name", None):
            extra[TASK_ATTR] = str(task)[:120]
        self._assembler.start_span(
            event, _SpanStartSpec(spec.name, spec.span_kind, {**spec.attributes, **extra})
        )

    def _starter(self, scope: _Scope) -> Callable[[Any, Any], None]:
        def start(source: Any, event: Any) -> None:
            if self._is_suppressed():
                return
            attributes: dict[str, Any] = {}
            if (value := scope.input(event)) is not None:
                attributes["input.value"] = _short(value)
            if role := getattr(event, "agent_role", None):
                attributes[AGENT_ATTR] = role
            self._assembler.start_span(event, _SpanStartSpec(scope.name(event), scope.kind, attributes))

        return start

    def _ender(self, scope: _Scope, failed: bool) -> Callable[[Any, Any], None]:
        def end(source: Any, event: Any) -> None:
            if self._is_suppressed():
                return
            error = (getattr(event, "error", None) or "failed") if failed else scope.end_error(event)
            output = None if error else scope.output(event)
            self._assembler.end_span(
                event, _SpanEndSpec(output=output, error=error, attributes=scope.attrs(event))
            )

        return end

    def _on_hook(self, source: Any, event: HookDispatchedEvent) -> None:
        if self._is_suppressed() or event.outcome == "proceeded":
            return
        attributes = {
            "hook.point": event.interception_point,
            "hook.outcome": event.outcome,
            "hook.count": event.hook_count,
            "hook.duration_ms": event.duration_ms,
        }
        if event.abort_source:
            attributes["hook.abort_source"] = event.abort_source
        self._assembler.start_span(
            event,
            _SpanStartSpec(f"hook {event.interception_point} {event.outcome}", Kind.GUARDRAIL, attributes),
        )
        error = f"{event.abort_source}: {event.abort_reason}" if event.outcome == "aborted" else None
        self._assembler.end_span(event, _SpanEndSpec(error=error))

    def _on_review_requested(self, source: Any, event: HumanFeedbackRequestedEvent) -> None:
        if self._is_suppressed():
            return
        with self._reviews_lock:
            self._reviews[(event.flow_name or "", event.method_name)] = event.event_id
        attributes = {"input.value": _short(event.message or ""), "review.outcomes": list(event.emit or [])}
        self._assembler.start_span(
            event, _SpanStartSpec(f"human review {event.method_name}", Kind.CHAIN, attributes)
        )

    def _on_review_received(self, source: Any, event: HumanFeedbackReceivedEvent) -> None:
        if self._is_suppressed():
            return
        with self._reviews_lock:
            requested = self._reviews.pop((event.flow_name or "", event.method_name), None)
        if requested is None:  # answered in another process (async review): nothing open here
            return
        # Not a crewai scope pair, so point the end at the request by hand.
        ended = SimpleNamespace(started_event_id=requested, timestamp=event.timestamp)
        attributes = {"review.outcome": event.outcome or "", "review.feedback_chars": len(event.feedback)}
        self._assembler.end_span(
            ended, _SpanEndSpec(output=event.feedback or "(empty: revise)", attributes=attributes)
        )


class RootSpans(SpanProcessor):
    """Remembers the trace id of every finished root span (a run's flow or crew), to print and report on."""

    def __init__(self) -> None:
        self.roots: list[tuple[str, str]] = []  # (trace id hex, root span name)

    def on_end(self, span: ReadableSpan) -> None:
        if span.parent is None and span.context is not None:
            self.roots.append((format(span.context.trace_id, "032x"), span.name))


@dataclass
class Tracing:
    provider: Any
    spans: CompintelSpans
    roots: RootSpans
    project: str
    url: str | None
    _previous_otel_env: str | None = field(default=None, repr=False)

    def flush(self) -> None:
        from crewai.events.event_bus import crewai_event_bus

        crewai_event_bus.flush()  # spans are made on the bus's handler threads
        self.provider.force_flush()

    def shutdown(self) -> None:
        self.flush()
        self.spans.shutdown()
        self.provider.shutdown()
        if self._previous_otel_env is None:
            os.environ.pop("OTEL_SDK_DISABLED", None)
        else:
            os.environ["OTEL_SDK_DISABLED"] = self._previous_otel_env


_active: Tracing | None = None


def active() -> Tracing | None:
    return _active


def enable(project: str = PROJECT, url: str = PHOENIX_URL, exporter: Any = None) -> Tracing:
    """Start exporting spans: to Phoenix at `url`, or to `exporter` (tests: an InMemorySpanExporter).

    config.apply_env() sets OTEL_SDK_DISABLED=true, which turns every OpenTelemetry provider into a no-op.
    It was there as crewai's telemetry opt-out, but CREWAI_DISABLE_TELEMETRY already covers that
    (crewai_core.telemetry checks either), so it is lifted for this process while tracing is on."""
    global _active
    if _active is not None:
        return _active
    previous = os.environ.get("OTEL_SDK_DISABLED")
    os.environ["OTEL_SDK_DISABLED"] = "false"
    if exporter is None:
        from phoenix.otel import register

        # Any: annotated as OpenInference's provider, it is phoenix.otel.TracerProvider (used below)
        provider: Any = register(
            endpoint=f"{url}/v1/traces",
            project_name=project,
            batch=True,
            set_global_tracer_provider=False,  # crewai's own tracing must not find it
            verbose=False,
        )
    else:
        from openinference.instrumentation import TracerProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"openinference.project.name": project}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    roots = RootSpans()
    if exporter is None:
        # Phoenix's TracerProvider *replaces* its exporter on add_span_processor() unless told not to: without
        # this flag the run is recorded and nothing reaches Phoenix (found live, M8b).
        provider.add_span_processor(roots, replace_default_processor=False)
    else:
        provider.add_span_processor(roots)
    target = url if exporter is None else None
    _active = Tracing(provider, CompintelSpans(tracer_provider=provider), roots, project, target, previous)
    return _active


def disable() -> Tracing | None:
    """Flush and stop; returns what was active (its `roots` say which traces were written)."""
    global _active
    current, _active = _active, None
    if current is not None:
        current.shutdown()
    return current


def trace_links(done: Tracing) -> list[str]:
    """One line per trace this process wrote: root span name and where to open it in Phoenix."""
    if done.url is None:
        return [f"{name}: trace {trace_id}" for trace_id, name in done.roots.roots]
    import time

    from phoenix.client import Client

    project_id = None
    for _ in range(10):  # a direct link needs the project's id; Phoenix creates it as the first spans land
        try:
            project_id = Client(base_url=done.url).projects.get(project_name=done.project)["id"]
            break
        except Exception:
            time.sleep(0.5)
    if project_id is None:
        where = f"(Phoenix {done.url}, project {done.project!r})"
        return [f"{name}: trace {trace_id} {where}" for trace_id, name in done.roots.roots]
    base = f"{done.url}/projects/{project_id}/traces"
    return [f"{name}: {base}/{trace_id}" for trace_id, name in done.roots.roots]


def require_phoenix(url: str = PHOENIX_URL) -> None:
    """Fail before a paid run if nothing is listening: spans would just be dropped."""
    import urllib.request

    try:
        urllib.request.urlopen(url, timeout=3).close()
    except OSError as e:
        raise RuntimeError(f"Phoenix isn't reachable at {url} ({e}). Start it: {PHOENIX_COMMAND}") from e
