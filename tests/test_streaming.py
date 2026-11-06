"""M8b streaming: `run_week(on_frame=...)` = Flow(stream=True) → crewai StreamSession → StreamPrinter. $0."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from compintel import config, hooks
from compintel.crews import short_update
from compintel.flow import run_week
from compintel.streaming import StreamPrinter
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import brief_for
from tests.test_flow import stubs as stubs  # noqa: F401  (fixture)
from tests.test_short_update_crew import REPORTS, Script, brief


def printer() -> tuple[StreamPrinter, io.StringIO]:
    out = io.StringIO()
    return StreamPrinter(Console(file=out, width=200, color_system=None)), out


REAL_SHORT_UPDATE = short_update.run  # taken before the `stubs` fixture replaces it


def with_real_crew(monkeypatch, llm: ScriptedLLM) -> None:
    """The routine crew for real (on a streaming fake), inside the stubbed week: its agents' tokens have to
    cross the flow's stream thread and crewai's task threads to reach the printer."""

    def run_short(week, reports, final=False, fake=None):
        REAL_SHORT_UPDATE(3, REPORTS, fake=llm)  # week-3 fixtures; the week's own brief is the stub's
        return SimpleNamespace(brief=brief_for(week, "routine"))

    monkeypatch.setattr(short_update, "run", run_short)


def test_a_streamed_week_prints_steps_and_tokens_and_still_publishes(world, stubs, monkeypatch):
    hooks.install()
    llm = ScriptedLLM(Rule(Script([brief()])), stream=True)
    with_real_crew(monkeypatch, llm)
    p, out = printer()
    flow = run_week(1, on_frame=p)
    text = out.getvalue()

    assert flow.state.published_path  # the stream changes how we watch, not what the run does
    assert p.tokens > 10 and p.tool_call_chunks >= 1
    for step in (
        "load_week",
        "scout_all",
        "assess_materiality",
        "run_short_update",
        "editorial_review",
        "publish",
    ):
        assert f"▶ {step}" in text and f"✓ {step}" in text
    assert "Market Analyst" in text and "Brief Writer" in text  # a header per streaming call
    assert "⇢ calls render_brief(…)" in text  # the writer's tool call, streamed by name
    assert "→ tool render_brief" in text  # and its execution
    assert '"headline"' in text  # the structured answer arrives as JSON text


def test_without_on_frame_nothing_streams(world, stubs):
    flow = run_week(1)
    assert flow.stream is False and flow.state.published_path


def test_stream_tokens_switches_every_llm_built_afterwards():
    assert config.make_llm().stream is False
    config.stream_tokens(True)
    try:
        assert config.make_llm(final=True).stream is True
    finally:
        config.stream_tokens(False)
    assert config.make_llm().stream is False


def test_the_printer_keeps_interleaved_calls_apart():
    p, out = printer()

    def chunk(call, role, text):
        return SimpleNamespace(
            type="llm_stream_chunk", channel="llm", data={"call_id": call, "agent_role": role, "chunk": text}
        )

    for frame in (
        chunk("a", "Scout (StockSathi)", "Price "),
        chunk("b", "Scout (BahiBook)", "GST "),
        chunk("a", "Scout (StockSathi)", "cut."),
    ):
        p(frame)
    p.finish()
    lines = out.getvalue().splitlines()
    assert lines == [
        "    Scout (StockSathi) ",
        "Price ",
        "    Scout (BahiBook) ",
        "GST ",
        "    Scout (StockSathi) ",
        "cut.",
    ]


def test_the_direct_llm_streaming_demo_streams_and_is_still_on_the_ledger():
    """scripts/stream_demo.py: chunk events from LLM.call, and every call still reaches the cost listener
    (crewai's scoped_handlers() would have hidden it)."""
    import importlib.util

    from compintel import listeners
    from compintel.cost import get_ledger
    from compintel.models import TieBreak

    path = Path(__file__).parents[1] / "scripts" / "stream_demo.py"
    spec = importlib.util.spec_from_file_location("stream_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    listeners.install()  # the cost listener, as bootstrap() installs it
    llm = ScriptedLLM(
        Rule(lambda m, rm: TieBreak(route="material", reason="40% cut.") if rm else "GST matters. A lot."),
        stream=True,
    )
    out = io.StringIO()
    seen = module.demo(llm, write=out.write)
    assert seen["text"] >= 3 and "GST matters." in out.getvalue() and '"route":"material"' in out.getvalue()
    assert len(get_ledger().path.read_text().splitlines()) == 3
