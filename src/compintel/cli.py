"""`compintel` command line. M0: hello, costs. M2: scout. M3: brief. M4: deepdive. M5: run, plot."""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager

import typer
from rich.console import Console
from rich.table import Table

from compintel.config import get_settings, model_id

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _bootstrap() -> None:
    from compintel.runtime import bootstrap

    bootstrap()


@app.command()
def hello(
    headline: str = typer.Argument(None, help="Headline to classify (default: the week-2 price cut)."),
    final: bool = typer.Option(False, "--final", help="Use MODEL_FINAL (Sonnet) instead of MODEL_DEV."),
    verbose: bool = typer.Option(False, "--verbose", help="crewai's agent verbose output."),
) -> None:
    """ONE live call: a single Agent.kickoff with a typed response_format. Haiku: well under $0.01."""
    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _bootstrap()
    from crewai.events.event_bus import crewai_event_bus

    from compintel import hello as hello_mod
    from compintel.config import make_llm
    from compintel.cost import get_ledger

    before = get_ledger().spent_usd
    out = hello_mod.run(headline or hello_mod.DEFAULT_HEADLINE, llm=make_llm(final), verbose=verbose)
    crewai_event_bus.flush()  # the cost row is written on the bus's thread pool

    console.print(f"[green]model[/green] {model_id(final)}")
    console.print_json(out.pydantic.model_dump_json() if out.pydantic else json.dumps(out.raw))
    console.print(f"[dim]usage_metrics (crewai's own count): {out.usage_metrics}[/dim]")
    after = get_ledger().spent_usd
    console.print(
        f"[yellow]this run:[/yellow] ${after - before:.6f}   [yellow]P3 total:[/yellow] ${after:.6f}"
    )


def _require_fakeweb() -> None:
    import urllib.request

    url = f"{get_settings().fakeweb_url}/week-1/stocksathi/pricing.html"
    try:
        urllib.request.urlopen(url, timeout=3).close()
    except OSError as e:
        raise typer.BadParameter(
            f"fake web not reachable at {url} ({e}). Start it: uv run python scripts/serve_fakeweb.py"
        ) from e


@app.command()
def scout(
    week: int = typer.Option(..., "--week", min=1, max=3, help="Week to scout."),
    via: str = typer.Option(
        "agents", "--via", help="agents = 3x Agent.kickoff_async; crew = kickoff_for_each."
    ),
    verbose: bool = typer.Option(False, "--verbose", help="crewai's agent verbose output."),
) -> None:
    """Live: scout all three competitors for a week, then check against ground truth. ~$0.01 on Haiku."""
    if via not in ("agents", "crew"):
        raise typer.BadParameter("--via must be 'agents' or 'crew'")
    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _require_fakeweb()
    _bootstrap()
    import asyncio
    import time

    from crewai.events.event_bus import crewai_event_bus

    from compintel import scouting
    from compintel.cost import get_ledger
    from compintel.world.ground_truth import check_week

    ledger = get_ledger()
    before, rows_before = ledger.spent_usd, _ledger_rows()
    started = time.perf_counter()
    if via == "agents":
        reports = asyncio.run(scouting.scout_all(week, verbose=verbose))
    else:
        reports = scouting.scout_all_crew(week, verbose=verbose)
    elapsed = time.perf_counter() - started
    crewai_event_bus.flush()

    out = get_settings().p3_runs_dir / "scout" / f"week-{week}.{via}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({c: r.model_dump() for c, r in reports.items()}, indent=2, ensure_ascii=False))

    table = Table("id", "kind", "impact", "novelty", "cred", "detail", show_lines=False)
    for r in reports.values():
        for s in r.signals:
            table.add_row(s.id, s.kind, s.impact_on_us, s.novelty, f"{s.source_credibility:.2f}", s.detail)
    console.print(table)

    for group, matches in check_week(reports, week).items():
        for m in matches:
            mark = "[green]✓[/green]" if m.found else "[red]✗[/red]"
            kind = "" if not m.found or m.kind_ok else f" [yellow](kind: {m.signal.kind})[/yellow]"  # type: ignore[union-attr]
            console.print(f"{mark} {group}: {m.expected['company']} {m.expected['detail_contains']}{kind}")
    calls = _ledger_rows() - rows_before
    console.print(f"[dim]saved {out}[/dim]")
    console.print(
        f"[yellow]via {via}: {calls} LLM calls, {elapsed:.1f}s, ${ledger.spent_usd - before:.6f}[/yellow]   "
        f"[yellow]P3 total:[/yellow] ${ledger.spent_usd:.6f}"
    )


@app.command()
def brief(
    week: int = typer.Option(..., "--week", min=1, max=3, help="Week to write the routine brief for."),
    final: bool = typer.Option(False, "--final", help="Brief Writer on MODEL_FINAL (Sonnet)."),
    render_gate: bool = typer.Option(
        False, "--render-gate", help="Ask before the writer renders (hook-based HITL, spec §10.3)."
    ),
) -> None:
    """Live: run ShortUpdateCrew on saved scout reports (runs/scout/week-N.agents.json). ~$0.005 on Haiku."""
    import os

    from compintel.config import PROJECT_ROOT

    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    reports = _load_scout_reports(week)
    _bootstrap()
    # crewai's output_file is cwd-relative (inputs can't make it absolute); like `crewai run`, use the root.
    os.chdir(PROJECT_ROOT)
    import time

    from crewai.events.event_bus import crewai_event_bus

    from compintel.cost import get_ledger
    from compintel.crews import short_update
    from compintel.listeners import GuardrailLog

    if render_gate:
        from compintel import hooks

        hooks.install_render_gate()
    log = GuardrailLog()
    ledger = get_ledger()
    before, rows_before, started = ledger.spent_usd, _ledger_rows(), time.perf_counter()
    result = short_update.run(week, reports, final=final)
    elapsed = time.perf_counter() - started
    crewai_event_bus.flush()

    console.print(result.markdown)
    for e in log.entries:
        mark = "[green]pass[/green]" if e["passed"] else "[red]FAIL → retry[/red]"
        detail = "" if e["passed"] else f": {e['error']}"
        console.print(f"guardrail {e['type']:8} {e['name'][:60]!r} attempt {e['attempt']}: {mark}{detail}")
    console.print(f"[dim]wrote briefs/drafts/week-{week:02d}.md and .json[/dim]")
    console.print(
        f"[yellow]{_ledger_rows() - rows_before} LLM calls, {elapsed:.1f}s, ${ledger.spent_usd - before:.6f}"
        f"[/yellow]   [yellow]P3 total:[/yellow] ${ledger.spent_usd:.6f}"
    )


def _load_scout_reports(week: int) -> dict:
    """The week's scout reports: from `compintel scout` (runs/scout/), else from the week's published Flow
    state, which keeps the reports it ran on (no need to scout again after `compintel run`)."""
    from compintel.models import SignalReport

    scout_file = get_settings().p3_runs_dir / "scout" / f"week-{week}.agents.json"
    if scout_file.exists():
        return {c: SignalReport.model_validate(r) for c, r in json.loads(scout_file.read_text()).items()}
    _bootstrap()  # env before crewai's import
    from crewai.flow.persistence import SQLiteFlowPersistence

    from compintel.flow import read_state_index
    from compintel.models import CIState

    state_id = read_state_index()["published"].get(str(week))
    data = SQLiteFlowPersistence().load_state(state_id) if state_id else None
    reports = CIState.model_validate(data).reports if data else {}
    if not reports:
        hint = f"run `compintel scout --week {week}` or `compintel run --week {week}`"
        raise typer.BadParameter(f"no scout reports for week {week}: {hint}")
    console.print(f"[dim]scout reports from week {week}'s published run (state {state_id})[/dim]")
    return reports


@app.command()
def deepdive(
    week: int = typer.Option(
        2, "--week", min=1, max=3, help="Week to deep-dive (week 2 is the material one)."
    ),
    variant: str = typer.Option(
        "manager_agent", "--variant", help="sequential | manager_llm | manager_agent"
    ),
    final: bool = typer.Option(False, "--final", help="Brief Writer on MODEL_FINAL (Sonnet)."),
    verbose: bool = typer.Option(False, "--verbose", help="crewai's agent verbose output."),
    trace: bool = typer.Option(False, "--trace", help="Send the run's trace to a local Phoenix (M8b)."),
) -> None:
    """Live: run DeepDiveCrew on saved scout reports. Haiku: a few cents (see README for per-variant cost)."""
    import os

    from compintel.config import PROJECT_ROOT
    from compintel.crews.deep_dive import VARIANTS

    if variant not in VARIANTS:
        raise typer.BadParameter(f"--variant must be one of {', '.join(VARIANTS)}")
    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    reports = _load_scout_reports(week)
    _require_fakeweb()  # the pricing and product analysts call pricing_parse / changelog_diff
    _bootstrap()
    os.chdir(PROJECT_ROOT)  # crewai's output paths are cwd-relative (see `brief`)
    import time

    from crewai.events.event_bus import crewai_event_bus

    from compintel.cost import get_ledger
    from compintel.crews import deep_dive
    from compintel.listeners import DelegationLog, FailureLog, GuardrailLog
    from compintel.tools.render_brief import render_markdown
    from compintel.world.ground_truth import check_brief

    guard_log, deleg_log, failures = GuardrailLog(), DelegationLog(), FailureLog()
    ledger = get_ledger()
    before, rows_before, started = ledger.spent_usd, _ledger_rows(), time.perf_counter()
    with _traced(trace):
        result = deep_dive.run(week, reports, variant=variant, final=final, verbose=verbose)
    elapsed = time.perf_counter() - started
    crewai_event_bus.flush()

    console.print(render_markdown(result.brief))
    console.print(
        "[bold]Escalation memo[/bold]: "
        + ("skipped (no high threat)" if result.escalation_memo is None else "")
    )
    if result.escalation_memo:
        console.print(result.escalation_memo)
    for e in guard_log.entries:
        mark = "[green]pass[/green]" if e["passed"] else "[red]FAIL → retry[/red]"
        console.print(f"guardrail {e['type']:8} {e['name'][:50]!r} attempt {e['attempt']}: {mark}")
    for e in deleg_log.entries:
        console.print(f"delegation: {e['by']} → {e['kind']} → {e['coworker']}")
    console.print(f"ground truth: {check_brief(result.brief, reports, week)}")
    console.print(
        f"recovered failures: {len(failures.llm_call_failures)} LLM calls, "
        f"{len(failures.observation_failures)} planning step observations"
    )
    console.print(
        f"[yellow]{variant}: {_ledger_rows() - rows_before} LLM calls, {elapsed:.1f}s, "
        f"${ledger.spent_usd - before:.6f}[/yellow]   [yellow]P3 total:[/yellow] ${ledger.spent_usd:.6f}"
    )


@app.command()
def run(
    week: int = typer.Option(..., "--week", min=1, max=3, help="Week to run end to end."),
    variant: str = typer.Option(
        "manager_agent", "--variant", help="Deep-dive variant, if the week is material."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Resume this week's crashed run instead of starting over."
    ),
    review_mode: str = typer.Option(
        "console",
        "--review",
        help="console: review in this terminal (blocking). async: pause, answer with `compintel review`.",
    ),
    live: bool = typer.Option(
        None, "--live/--no-live", help="Live status panel on stderr (default: on in a terminal)."
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Print steps, tool calls and the models' tokens as they happen (no panel)."
    ),
    trace: bool = typer.Option(False, "--trace", help="Send the run's trace to a local Phoenix (M8b)."),
) -> None:
    """Live: the whole week through the Flow (scout, route, crew, review, publish). Weeks 2+ fork the week
    before."""
    import os

    from compintel.config import PROJECT_ROOT

    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _require_fakeweb()
    _bootstrap()
    os.chdir(PROJECT_ROOT)  # briefs/ and the crews' output paths are cwd-relative
    from compintel import review
    from compintel.flow import run_week

    if review_mode not in ("console", "async"):
        raise typer.BadParameter("--review must be console or async")
    reviewer = review.PendingReview() if review_mode == "async" else review.ConsoleReview()
    printer = None
    if stream:
        from compintel.config import stream_tokens
        from compintel.streaming import StreamPrinter

        stream_tokens(True)  # LLMs built from here on stream; the crews build theirs during the run
        printer, live = StreamPrinter(console), False  # the panel would redraw over the streamed text
    with _traced(trace), _measure(f"week {week}", live=live) as done:
        try:
            flow = run_week(
                week, deep_dive_variant=variant, resume=resume, reviewer=reviewer, on_frame=printer
            )
        except ValueError as e:  # refused before anything ran: which week to run or resume first
            raise typer.BadParameter(str(e)) from e
        done(flow)


@app.command()
def revise(
    week: int = typer.Option(..., "--week", min=1, max=3, help="Published week whose brief to revise."),
    feedback: str = typer.Argument(..., help="What to change."),
    human_input: bool = typer.Option(
        False, "--human-input", help="Task human_input=True: crewai asks you for more rounds (spec §10.3)."
    ),
) -> None:
    """Live: RevisionCrew alone on a published brief, printed, not published. Crew-level HITL to compare with
    the Flow's @human_feedback review. ~$0.005."""
    import os

    from compintel.config import PROJECT_ROOT

    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _bootstrap()
    os.chdir(PROJECT_ROOT)
    from crewai.flow.persistence import SQLiteFlowPersistence

    from compintel.crews import revision, short_update
    from compintel.flow import read_state_index
    from compintel.models import CIState
    from compintel.tools.render_brief import render_markdown

    state_id = read_state_index()["published"].get(str(week))
    data = SQLiteFlowPersistence().load_state(state_id) if state_id else None
    if not data:
        raise typer.BadParameter(f"week {week} isn't published; run it first")
    state = CIState.model_validate(data)
    assert state.brief is not None
    revised = revision.run(
        state.brief, feedback, [], short_update.signals_input(state.reports), human_input=human_input
    )
    console.print(render_markdown(revised))


@app.command()
def chat(
    session: str = typer.Option(None, "--session", help="Continue an earlier conversation (its session id)."),
    ask: str = typer.Option(None, "--ask", help="Ask one question and exit."),
) -> None:
    """Live: Ask the Analyst, a conversational Flow over memory and the published briefs. ~$0.002 a turn.
    Type 'exit' to stop."""
    import os

    from compintel.config import PROJECT_ROOT

    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _bootstrap()
    os.chdir(PROJECT_ROOT)  # briefs/ is cwd-relative
    from compintel.chat import AskAnalyst
    from compintel.cost import get_ledger

    ledger = get_ledger()
    before = ledger.spent_usd
    flow = AskAnalyst()
    while True:
        message = ask or console.input("[bold]you>[/bold] ").strip()
        if message.lower() in ("exit", "quit"):
            break
        if not message:
            continue
        answer = flow.handle_turn(message, session_id=session)
        session = flow.state.id  # the first turn creates the session; later turns continue it
        console.print(f"[bold cyan]analyst ({flow.state.last_intent})>[/bold cyan]")
        console.print(str(answer), markup=False)  # citations like [ShelfMitra-3-3] aren't rich markup
        if ask or flow.state.ended:
            break
    console.print(
        f"[dim]session {session} (continue: compintel chat --session {session})[/dim]   "
        f"[yellow]${ledger.spent_usd - before:.6f}, P3 total ${ledger.spent_usd:.6f}[/yellow]"
    )


@app.command(name="memory")
def memory_cmd(
    reset: bool = typer.Option(False, "--reset", help="Delete everything in memory (asks first)."),
) -> None:
    """What CompIntel remembers: the scope tree of runs/memory, with record counts. $0."""
    _bootstrap()
    from compintel import memory

    store = memory.shared()
    if reset:
        typer.confirm(f"Delete every memory in {memory.memory_dir()}?", abort=True)
        store.reset_all()
        console.print("memory cleared")
        return
    console.print(store.tree("/", max_depth=3))


@app.command()
def pending() -> None:
    """Briefs waiting for an async review (`run --review async`). $0."""
    from compintel import review

    waiting = review.list_pending()
    if not waiting:
        console.print("no pending reviews")
    for p in waiting:
        console.print(f"[bold]week {p['week']}[/bold]  {p['flow_id']}  (since {p['requested_at'][:19]})")
        console.print(p["markdown"])
        console.print(f'[dim]{p["message"]}  →  compintel review {p["flow_id"]} "<your feedback>"[/dim]\n')


@app.command(name="review")
def review_cmd(
    flow_id: str = typer.Argument(..., help="From `compintel pending`."),
    feedback: str = typer.Argument(
        "", help="Free text; mapped to approve / revise / reject. Empty = revise."
    ),
    live: bool = typer.Option(
        None, "--live/--no-live", help="Live status panel on stderr (default: on in a terminal)."
    ),
    trace: bool = typer.Option(False, "--trace", help="Send the run's trace to a local Phoenix (M8b)."),
) -> None:
    """Live: answer a pending review; the week continues in this process (revise → another pending review)."""
    import os

    from compintel.config import PROJECT_ROOT

    if not get_settings().anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set; put it in .env")
    _bootstrap()
    os.chdir(PROJECT_ROOT)
    from compintel import review

    with _traced(trace), _measure("review", live=live) as done:
        try:
            flow, _ = review.resume(flow_id, feedback)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
        done(flow)


@contextmanager
def _traced(on: bool):
    """Export this block's spans to Phoenix, then print where to open each trace. Nothing when `on` is
    False."""
    if not on:
        yield
        return
    from compintel import tracing

    try:
        tracing.require_phoenix()
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    tracing.enable()
    try:
        yield
    finally:
        done = tracing.disable()  # flushes: the last spans are sent before the links are printed
        for line in tracing.trace_links(done) if done else []:
            console.print(f"[bold]trace[/bold] {line}", soft_wrap=True)  # a wrapped URL can't be clicked


@contextmanager
def _measure(label: str, live: bool | None = None):
    """Time and cost a Flow run, then print its outcome; the body hands over the flow with `done(flow)`.

    `live`: show the RunMonitor's live panel while the run lasts (None: only when stderr is a terminal)."""
    import sys
    import time
    from contextlib import nullcontext

    from crewai.events.event_bus import crewai_event_bus
    from crewai.hooks import HookAborted

    from compintel import hooks, listeners
    from compintel.cost import get_ledger

    ledger = get_ledger()
    before, rows_before, started = ledger.spent_usd, _ledger_rows(), time.perf_counter()
    steps_before = len(hooks.step_trace.entries)
    monitor = listeners.monitor()
    show_live = (sys.stderr.isatty() if live is None else live) and monitor is not None
    flows: list = []
    aborted: HookAborted | None = None
    with listeners.LiveConsole(monitor) if show_live and monitor else nullcontext():
        try:
            yield flows.append
        except HookAborted as e:  # a hook stopped the whole run (e.g. budget-gate at execution start)
            aborted = e
        crewai_event_bus.flush()  # let the last events reach the panel before it closes
    elapsed = time.perf_counter() - started
    from compintel.runtime import restore_std_streams

    restore_std_streams()  # a deep dive can leave sys.stdout on crewai's StringIO (see there)
    if aborted is not None:
        _print_observed(monitor, hooks.step_trace.entries[steps_before:])
        console.print(f"[red]stopped by a hook[/red] ({aborted.source}): {aborted.reason}")
        raise typer.Exit(1)
    _print_week(flows[0], _ledger_rows() - rows_before)
    _print_observed(monitor, hooks.step_trace.entries[steps_before:])
    console.print(
        f"[yellow]{label}: {elapsed:.1f}s, ${ledger.spent_usd - before:.6f}[/yellow]   "
        f"[yellow]P3 total:[/yellow] ${ledger.spent_usd:.6f}"
    )


def _print_observed(monitor, steps: list[dict]) -> None:
    """What the hooks did and where the time went (from RunMonitor and step_tracer)."""
    if monitor is not None:
        h = monitor.hook_summary()
        console.print(
            f"[bold]hooks[/bold] {h.get('dispatched', 0)} dispatches: {h.get('proceeded', 0)} proceeded, "
            f"{h.get('modified', 0)} modified, {h.get('aborted', 0)} aborted"
        )
        for line in monitor.hook_aborts:
            console.print(f"  [red]abort[/red] {line}")
    timed = sorted((s for s in steps if s["seconds"] is not None), key=lambda s: -s["seconds"])
    if timed:
        slowest = ", ".join(f"{s['step']} {s['seconds']:.0f}s" for s in timed[:4])
        console.print(f"[bold]steps[/bold] {len(steps)} traced (runs/steps.jsonl); slowest: {slowest}")


def _print_week(flow, calls: int) -> None:
    from compintel.flow import is_paused

    s = flow.state
    if is_paused(flow):
        console.print(
            f"[bold yellow]paused for review[/bold yellow]: compintel pending · compintel review {s.id}"
        )
    elif s.published_path:
        console.print(s.brief_markdown)
        console.print(f"published {s.published_path} (reviews: briefs/week-{s.week:02d}.feedback.json)")
    else:
        console.print(f"[red]rejected[/red]: archived in briefs/rejected/week-{s.week:02d}.md")
    console.print(
        f"[bold]route[/bold] {s.route} (score {s.materiality_score}): {'; '.join(s.materiality_reasons)}"
    )
    console.print(
        f"state id {s.id}" + (f", forked from {s.previous_state_id}" if s.previous_state_id else "")
    )
    for r in s.feedback_log:
        lessons = " [lessons applied before review]" if r["changed_by_lessons"] else ""
        console.print(f"review {r['round']}: {r['outcome']} ← {r['feedback']!r}{lessons}")
    # flow.usage_metrics: every LLM call in the run, all crews included (a crew's kickoff().token_usage
    # isn't). After a resumed review it covers this process only.
    usage = flow.usage_metrics
    console.print(
        f"flow.usage_metrics: {usage.total_tokens:,} tokens in {usage.successful_requests} requests "
        f"(cost ledger: {calls} calls)"
    )


@app.command()
def traces(
    trace_id: list[str] = typer.Option(None, "--trace-id", help="Only these traces (default: every one)."),
    last: int = typer.Option(0, "--last", help="Only the N most recent traces."),
    write: bool = typer.Option(
        False, "--write", help="Put the report into docs/observability.md (between its markers)."
    ),
) -> None:
    """Summarize traced runs from the local Phoenix: delegations, fan-out, guardrails, hooks, memory, review,
    per-agent token share. $0."""
    from compintel import trace_report, tracing
    from compintel.config import PROJECT_ROOT

    try:
        tracing.require_phoenix()
        found = trace_report.group_traces(trace_report.fetch(tracing.PHOENIX_URL, tracing.PROJECT, trace_id))
    except Exception as e:
        raise typer.BadParameter(str(e)) from e
    found = found[-last:] if last else found
    if not found:
        console.print(f"no traces in Phoenix project {tracing.PROJECT!r}; run something with --trace first")
        return
    markdown = trace_report.report(found)
    if write:
        doc = PROJECT_ROOT / "docs" / "observability.md"
        trace_report.write_into(doc, markdown)
        console.print(f"wrote the report for {len(found)} traces into {doc}")
    else:
        print(markdown)  # raw markdown: rich would re-wrap the tables


@app.command()
def plot() -> None:
    """Write docs/flow_plot.html: the Flow's graph, every router branch labelled. $0."""
    from compintel.flow import plot as write_plot

    _bootstrap()
    write_plot()


def _ledger_rows() -> int:
    path = get_settings().p3_runs_dir / "costs.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


@app.command()
def costs() -> None:
    """P3 spend so far, per model and agent, from runs/costs.jsonl."""
    s = get_settings()
    ledger = s.p3_runs_dir / "costs.jsonl"
    if not ledger.exists():
        console.print("no spend recorded yet")
        return
    totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0, 0, 0.0])
    for line in ledger.read_text().splitlines():
        r = json.loads(line)
        t = totals[(r["model"], r.get("agent") or "-")]
        t[0] += 1
        t[1] += r["input_tokens"] + r.get("cache_read_tokens", 0) + r.get("cache_write_tokens", 0)
        t[2] += r["output_tokens"]
        t[3] += r["cost_usd"]
    table = Table("model", "agent", "calls", "input tok", "output tok", "USD")
    for (model, agent), (n, i, o, usd) in sorted(totals.items()):
        table.add_row(model, agent, str(n), str(i), str(o), f"{usd:.6f}")
    console.print(table)
    spent = sum(t[3] for t in totals.values())
    console.print(f"[yellow]P3 total ${spent:.6f} of ${s.p3_spend_cap_usd:.2f} cap[/yellow]")
