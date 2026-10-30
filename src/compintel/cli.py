"""`compintel` command line. M0: hello, costs. M2: scout."""

from __future__ import annotations

import json
from collections import defaultdict

import typer
from rich.console import Console
from rich.table import Table

from compintel.config import apply_env, get_settings, model_id

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _bootstrap() -> None:
    """Env first (crewai reads telemetry/MODEL vars on import), then the global listener and hooks."""
    apply_env()
    from compintel import hooks, listeners

    listeners.install()
    hooks.install()


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
