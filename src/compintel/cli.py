"""`compintel` command line. M0 commands: hello, costs."""

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
