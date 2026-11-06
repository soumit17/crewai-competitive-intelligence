"""M8b tracing: crewai runs → OpenTelemetry spans (what Phoenix receives), read back by the trace report.

Spans go to an in-memory exporter and are converted to the dicts Phoenix's client returns, so the same
functions summarize them as in `compintel traces`. One test per item docs/observability.md must show
(spec §15). $0.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pytest
import typer
from crewai.events.event_bus import crewai_event_bus
from crewai.hooks import HookAborted
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compintel import hooks, trace_report, tracing
from compintel.cost import get_ledger
from compintel.crews import deep_dive, short_update
from compintel.trace_report import Trace, group_traces
from tests.deep_dive_script import WEEK2_REPORTS, DeepDiveScript
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import stubs as stubs  # noqa: F401  (fixture)
from tests.test_short_update_crew import REPORTS, Script, brief


@pytest.fixture
def exported(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")  # as conftest and apply_env() leave it
    exporter = InMemorySpanExporter()
    tracing.enable(exporter=exporter)
    yield exporter
    tracing.disable()


def as_phoenix(s) -> dict:
    """An exported span in the shape `phoenix.client` returns."""

    def iso(ns):
        return datetime.fromtimestamp(ns / 1e9, UTC).isoformat()

    return {
        "name": s.name,
        "span_kind": s.attributes.get("openinference.span.kind", "UNKNOWN"),
        "context": {"trace_id": f"{s.context.trace_id:032x}", "span_id": f"{s.context.span_id:016x}"},
        "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
        "start_time": iso(s.start_time),
        "end_time": iso(s.end_time),
        "status_code": s.status.status_code.name,
        "status_message": s.status.description or "",
        "attributes": dict(s.attributes),
    }


def traces_of(exporter: InMemorySpanExporter) -> list[Trace]:
    crewai_event_bus.flush()  # spans are made on the bus's handler threads
    return group_traces([as_phoenix(s) for s in exporter.get_finished_spans()])


def routine_crew(*briefs) -> None:
    hooks.install()
    short_update.run(3, REPORTS, fake=ScriptedLLM(Rule(Script(list(briefs) or [brief()]))))


def roles(t: Trace, kind: str = "AGENT") -> list[str]:
    return [trace_report._role(s) for s in t.kind(kind)]


# ---------------------------------------------------------------- the base: crew → agents → LLM calls, tools
def test_a_crew_run_is_one_trace_of_agents_llm_calls_and_tools(exported):
    routine_crew()
    [t] = traces_of(exported)
    assert t.root["name"] == "Short Update Crew.kickoff"
    assert {"Market Analyst", "Brief Writer"} <= set(roles(t))
    assert any(s["name"] == "render_brief.run" for s in t.kind("TOOL"))
    llm = t.kind("LLM")
    assert len(llm) == 4  # classify, write, the rumor guardrail's judge, render
    for s in llm:  # who made the call, and what it used: the base of the per-agent token share
        assert s["attributes"][tracing.AGENT_ATTR] in {"Market Analyst", "Brief Writer", "Guardrail Agent"}
        assert s["attributes"]["llm.token_count.total"] > 0
        assert s["attributes"]["llm.model_name"] == "claude-haiku-5-5"
    active = tracing.active()
    assert active is not None and active.roots.roots == [(t.root["context"]["trace_id"], t.root["name"])]


def test_the_llm_call_inside_a_guardrail_is_exported_under_it(exported):
    """Why CompintelSpans covers every scope: the assembler parks a child until its parent's span exists, so
    without a guardrail span the judge's LLM call would never be exported."""
    routine_crew()
    [t] = traces_of(exported)
    [judge] = [s for s in t.kind("LLM") if s["attributes"][tracing.AGENT_ATTR] == "Guardrail Agent"]
    assert any(a["span_kind"] == "GUARDRAIL" for a in t.ancestors(judge))


# ---------------------------------------------------------------- the six things the doc must show
def test_a_guardrail_retry_is_a_failed_check_then_a_passing_one(exported):
    routine_crew(brief(tl_dr=["BahiBook is hiring a PM"]), brief())
    [t] = traces_of(exported)
    cites = [g for g in trace_report.guardrails(t) if g["name"] == "every_claim_cites_signal"]
    assert [g["passed"] for g in cites] == [False, True]
    assert "tl_dr[0] cites no signal id" in cites[0]["message"]
    assert roles(t).count("Brief Writer") == 3  # write, write again after the failed check, render
    assert "1 failed → retried" in trace_report.report([t])


def test_a_hook_abort_is_an_error_span_with_its_source(exported, world, stubs):
    from compintel.flow import run_week

    get_ledger().record("claude-haiku-5-5", {"input_tokens": 200_000_000}, response_id="big")  # $20 > $12
    hooks.install()
    with pytest.raises(HookAborted):
        run_week(1)
    [t] = traces_of(exported)
    [hook] = trace_report.hook_spans(t)
    assert hook["name"] == "hook execution_start aborted" and hook["status_code"] == "ERROR"
    assert hook["status_message"] == "budget-gate: P3 spend cap reached: $20.0000 of $12.00"
    assert hook["attributes"]["hook.abort_source"] == "budget-gate"


def deep(variant: str, delay: float = 0.0) -> None:
    script = DeepDiveScript()

    def reply(messages, response_model):
        time.sleep(delay)  # lets concurrent tasks visibly overlap
        return script(messages, response_model)

    deep_dive.run(2, WEEK2_REPORTS, variant=variant, fake=ScriptedLLM(Rule(reply)))


def test_manager_delegations_nest_each_specialist_under_the_managers_tool_call(exported, world):
    hooks.install()
    deep("manager_agent")
    [t] = traces_of(exported)
    d = trace_report.delegations(t)
    assert [x["coworker"] for x in d] == [
        "Pricing Analyst",
        "Product Analyst",
        "Market Analyst",
        "Strategist",
        "Strategist",
        "Brief Writer",
    ]
    assert {x["manager"] for x in d} == {"Head of Strategy"}
    assert {x["tool"] for x in d} == {"delegate_work_to_coworker"}


def test_async_tasks_fan_out_as_overlapping_agent_spans(exported, world):
    hooks.install()
    deep("sequential", delay=0.05)
    [t] = traces_of(exported)
    [group] = trace_report.fan_out(t)
    analysts = sorted(trace_report._role(s) for s in group)
    assert analysts == ["Market Analyst", "Pricing Analyst", "Product Analyst"]
    assert "█" in "\n".join(trace_report._timeline(group))


def test_a_flow_week_traces_every_step_memory_and_the_human_review(exported, world, stubs, monkeypatch):
    from compintel import review
    from compintel.flow import run_week
    from tests import hitl

    hitl.install(monkeypatch, hitl.ScriptedReviewer(("Looks good, ship it.", "approve")))
    monkeypatch.setattr("builtins.input", lambda *_: "Looks good, ship it.")
    hooks.install()
    run_week(1, reviewer=review.ConsoleReview())
    [t] = traces_of(exported)
    assert t.root["name"] == "CompetitiveIntelFlow.kickoff"
    methods = {s["attributes"].get("flow.node.name") for s in t.spans}
    steps = {
        "load_week",
        "scout_all",
        "assess_materiality",
        "run_short_update",
        "editorial_review",
        "publish",
    }
    assert steps <= methods
    [r] = trace_report.reviews(t)
    assert r["attributes"]["output.value"] == "Looks good, ship it."
    assert t.by_id[r["parent_id"]]["attributes"]["flow.node.name"] == "editorial_review"
    memory = trace_report.memory_counts(t)
    assert memory["memory.save"][0] >= 1 and memory["memory.recall"][0] >= 1  # publish saves; review recalls
    text = trace_report.report([t])
    assert "**Human review**: waited" in text and "memory.save" in text


def test_per_agent_token_share_adds_up(exported):
    routine_crew()
    rows = {r["agent"]: r for r in trace_report.token_share(traces_of(exported))}
    assert {"Market Analyst", "Brief Writer", "Guardrail Agent"} == set(rows)
    assert rows["Brief Writer"]["calls"] == 2
    assert sum(r["share"] for r in rows.values()) == pytest.approx(1.0)
    assert all(r["usd"] > 0 for r in rows.values())  # priced from compintel.cost


# ---------------------------------------------------------------- plumbing
def test_enable_lifts_otel_sdk_disabled_but_crewai_telemetry_stays_off(monkeypatch):
    from crewai_core.telemetry import Telemetry as CoreTelemetry

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    tracing.enable(exporter=InMemorySpanExporter())
    try:
        assert os.environ["OTEL_SDK_DISABLED"] == "false"
        assert CoreTelemetry._is_telemetry_disabled()  # CREWAI_DISABLE_TELEMETRY still says no
    finally:
        tracing.disable()
    assert os.environ["OTEL_SDK_DISABLED"] == "true" and tracing.active() is None


def test_the_phoenix_exporter_survives_our_root_span_processor(monkeypatch):
    """Phoenix's TracerProvider drops its exporter when another processor is added the plain way."""
    from phoenix.otel import BatchSpanProcessor

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    t = tracing.enable(url="http://127.0.0.1:9")  # built, never sent to: no span is made here
    try:
        processors = t.provider._active_span_processor._span_processors
        assert any(isinstance(p, BatchSpanProcessor) for p in processors)
        assert t.roots in processors
    finally:
        tracing.disable()


def test_tracing_needs_phoenix_listening():
    with pytest.raises(RuntimeError, match="uvx --from arize-phoenix"):
        tracing.require_phoenix("http://127.0.0.1:9")  # nothing listens on the discard port


def test_the_report_goes_between_the_docs_markers(tmp_path):
    doc = tmp_path / "observability.md"
    doc.write_text(f"intro\n{trace_report.START}\nold\n{trace_report.END}\noutro\n")
    trace_report.write_into(doc, "new report\n")
    assert doc.read_text() == f"intro\n{trace_report.START}\nnew report\n{trace_report.END}\noutro\n"


def test_a_hook_abort_ends_a_cli_run_with_one_line_not_a_traceback():
    from compintel import cli

    with pytest.raises(typer.Exit), cli._measure("week 1", live=False):
        raise HookAborted("P3 spend cap reached", source="budget-gate")


def test_deepdive_reads_the_scout_reports_from_the_published_week_when_none_are_saved(world, stubs):
    """`compintel deepdive --trace` after `compintel run`: no need to scout again."""
    from compintel import cli
    from compintel.flow import run_week

    run_week(1)
    assert cli._load_scout_reports(1) == stubs.reports[1]
    with pytest.raises(typer.BadParameter, match="no scout reports for week 2"):
        cli._load_scout_reports(2)
