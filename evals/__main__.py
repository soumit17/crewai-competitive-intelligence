"""`uv run python -m evals ...` (see evals/__init__.py)."""

from __future__ import annotations

import json

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def run(
    judge: bool = typer.Option(True, "--judge/--no-judge", help="Haiku judges (faithfulness, quality)."),
    final: bool = typer.Option(False, "--final", help="Quality judged by MODEL_FINAL (Sonnet)."),
    save_baseline: bool = typer.Option(False, "--save-baseline", help="Make this run evals/baseline.json."),
    gate: bool = typer.Option(
        False, "--gate", help="Exit 1 on a drop of more than 3 points vs the baseline."
    ),
    samples: int = typer.Option(3, "--samples", min=1, help="Calls per judge per week, averaged."),
) -> None:
    """Score the three published weeks; write evals/report.md and runs/evals/<time>.json."""
    from compintel.config import LONG_OUTPUT_MAX_TOKENS, get_settings, make_llm, model_id
    from compintel.cost import get_ledger
    from evals import faithfulness, scoring
    from evals.data import load_weeks

    try:
        runs = load_weeks()
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    if judge and not get_settings().anthropic_api_key:
        raise typer.BadParameter("the judges need ANTHROPIC_API_KEY (or run --no-judge)")
    faith = make_llm(max_tokens=LONG_OUTPUT_MAX_TOKENS) if judge else None
    qual = make_llm(final=final, max_tokens=LONG_OUTPUT_MAX_TOKENS) if judge else None
    # The sample count is part of the judge: an average of 3 isn't comparable with a single sample.
    judges = {
        "faithfulness": f"{model_id()} x{samples}, prompt v{faithfulness.PROMPT_VERSION}",
        "quality": f"{model_id(final)} x{samples}",
    }
    judges = judges if judge else {}

    from crewai.events.event_bus import crewai_event_bus

    before = get_ledger().spent_usd
    result = scoring.evaluate(runs, faith_llm=faith, quality_llm=qual, samples=samples)
    crewai_event_bus.flush()
    baseline = scoring.comparable(scoring.load_baseline(), judges)
    scoring.REPORT.write_text(scoring.markdown(result, judges, baseline))
    saved = scoring.save_run({**result, "judges": judges})
    for metric, value in result["scores"].items():
        shown = "-" if value is None else value
        before_value = baseline.get(metric)
        print(
            f"{metric:34} {shown:>6}" + (f"   (baseline {before_value})" if before_value is not None else "")
        )
    unmet = scoring.acceptance(result["scores"])
    print("M8 acceptance: " + ("met" if not unmet else "NOT met: " + "; ".join(unmet)))
    if result.get("agreement"):
        print(f"judge vs your scores: mean abs diff {result['agreement']['mean_abs_diff']}")
    spent = get_ledger().spent_usd
    print(f"report: {scoring.REPORT}   raw: {saved}   cost ${spent - before:.4f}, P3 total ${spent:.4f}")
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
def human(
    week: int = typer.Argument(None, min=1, max=3, help="Week to score (omit: show your scores so far)."),
    scores: list[int] = typer.Argument(
        None, help="Four 1-5 scores: actionable prioritized kolkata_focus concise."
    ),
) -> None:
    """Your blind 1-5 scores, kept in evals/human_scores.json. `human 2 4 3 5 3` scores week 2."""
    from evals import quality

    path = quality.HUMAN_SCORES
    data = quality.human_template([1, 2, 3])
    if path.exists():
        data.update({k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")})
    if week is not None:
        if not scores or len(scores) != len(quality.CRITERIA) or not all(1 <= x <= 5 for x in scores):
            raise typer.BadParameter(f"give four scores, 1-5: {' '.join(quality.CRITERIA)}")
        data[str(week)] = dict(zip(quality.CRITERIA, scores, strict=True))
    path.write_text(json.dumps(data, indent=2) + "\n")
    for w in ("1", "2", "3"):
        given = ", ".join(f"{c} {data[w][c] if data[w][c] is not None else '-'}" for c in quality.CRITERIA)
        print(f"week {w}: {given}")


if __name__ == "__main__":
    app()
