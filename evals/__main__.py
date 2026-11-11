"""`uv run python -m evals ...` (see evals/__init__.py)."""

from __future__ import annotations

import json

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def run(
    judge: bool = typer.Option(
        True, "--judge/--no-judge", help="Haiku judges (faithfulness, quality). ~$0.01."
    ),
    final: bool = typer.Option(
        False, "--final", help="Quality judged by MODEL_FINAL (Sonnet), once. ~$0.05."
    ),
    save_baseline: bool = typer.Option(False, "--save-baseline", help="Make this run evals/baseline.json."),
    gate: bool = typer.Option(
        False, "--gate", help="Exit 1 on a drop of more than 3 points vs the baseline."
    ),
) -> None:
    """Score the three published weeks; write evals/report.md and runs/evals/<time>.json."""
    from compintel.config import LONG_OUTPUT_MAX_TOKENS, get_settings, make_llm, model_id
    from compintel.cost import get_ledger
    from evals import scoring
    from evals.data import load_weeks

    try:
        runs = load_weeks()
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    if judge and not get_settings().anthropic_api_key:
        raise typer.BadParameter("the judges need ANTHROPIC_API_KEY (or run --no-judge)")
    faith = make_llm(max_tokens=LONG_OUTPUT_MAX_TOKENS) if judge else None
    qual = make_llm(final=final, max_tokens=LONG_OUTPUT_MAX_TOKENS) if judge else None
    judges = {"faithfulness": model_id(), "quality": model_id(final)} if judge else {}

    from crewai.events.event_bus import crewai_event_bus

    before = get_ledger().spent_usd
    result = scoring.evaluate(runs, faith_llm=faith, quality_llm=qual)
    crewai_event_bus.flush()
    baseline = scoring.comparable(scoring.load_baseline(), judges)
    scoring.REPORT.write_text(scoring.markdown(result, judges, baseline))
    saved = scoring.save_run({**result, "judges": judges})
    for metric, value in result["scores"].items():
        before_value = baseline.get(metric)
        print(
            f"{metric:34} {'-' if value is None else value:>6}"
            + (f"   (baseline {before_value})" if before_value is not None else "")
        )
    unmet = scoring.acceptance(result["scores"])
    print("M8 acceptance: " + ("met" if not unmet else "NOT met: " + "; ".join(unmet)))
    if result.get("agreement"):
        print(f"judge vs your scores: mean abs diff {result['agreement']['mean_abs_diff']}")
    print(
        f"report: {scoring.REPORT}   raw: {saved}   cost ${get_ledger().spent_usd - before:.4f}, "
        f"P3 total ${get_ledger().spent_usd:.4f}"
    )
    if save_baseline:
        if not judge:
            raise typer.BadParameter("a baseline needs the judges' metrics too: drop --no-judge")
        scoring.save_baseline(result, judges)
        print(f"baseline saved: {scoring.BASELINE}")
    if gate:
        drops = scoring.regressions(result["scores"], baseline)
        for d in drops:
            print(f"REGRESSION {d}")
        if drops:
            raise typer.Exit(1)
        print("gate: no metric dropped more than 3 points" if baseline else "gate: no baseline yet")


@app.command()
def human() -> None:
    """Write evals/human_scores.json for your blind 1-5 scores (keeps scores you already gave)."""
    from evals import quality

    path = quality.HUMAN_SCORES
    template = quality.human_template([1, 2, 3])
    if path.exists():
        existing = json.loads(path.read_text())
        template.update({k: v for k, v in existing.items() if not k.startswith("_")})
    path.write_text(json.dumps(template, indent=2) + "\n")
    print(f"fill in {path}: 1-5 per criterion per week, reading briefs/week-01.md .. week-03.md")


if __name__ == "__main__":
    app()
