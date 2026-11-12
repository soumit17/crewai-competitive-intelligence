"""Brief quality (spec §17.1): a rubric judge, your own blind scores, and the deterministic checks the judge
shouldn't be trusted with (threat levels vs ground truth, Kolkata leading in week 2).

The judge is Haiku by default and Sonnet with `--final` (the spec's one Sonnet pass). Your scores go in
`evals/human_scores.json` (`python -m evals human` writes the template): score before you look at the
judge's, so they're blind; the report then shows how far apart you are.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from compintel.config import PROJECT_ROOT
from compintel.tools.render_brief import render_markdown
from compintel.world.ground_truth import check_brief
from evals.data import WeekRun

CRITERIA = {
    "actionable": "Recommendations say who does what, by when, and follow from the signals.",
    "prioritized": "The biggest threat comes first; minor items don't crowd out major ones.",
    "kolkata_focus": "The threat to KiranaCloud's home market, Kolkata, is clear and given its due weight.",
    "concise": "Short, no padding or repetition; a busy reader gets it in two minutes.",
}
HUMAN_SCORES = PROJECT_ROOT / "evals" / "human_scores.json"


class RubricScore(BaseModel):
    actionable: int = Field(ge=1, le=5)
    prioritized: int = Field(ge=1, le=5)
    kolkata_focus: int = Field(ge=1, le=5)
    concise: int = Field(ge=1, le=5)
    comment: str = Field(description="Two sentences at most: the main strength and the main weakness.")


def judge_prompt(run: WeekRun) -> str:
    rubric = "\n".join(f"- {name} (1-5): {what}" for name, what in CRITERIA.items())
    return (
        "You review a weekly competitive-intelligence brief for KiranaCloud, a SaaS for kirana stores whose "
        "home market is Kolkata (Dukaan plan, ₹799/month). Score it 1 (poor) to 5 (excellent) on each "
        f"criterion. Be strict: 5 means nothing to improve.\n\n{rubric}\n\nThe brief:\n\n"
        f"{render_markdown(run.brief)}"
    )


def judge_week(run: WeekRun, llm: Any, samples: int = 3) -> dict:
    """The rubric, `samples` times; each criterion scores its mean (one sample moved by ~10 points live)."""
    outs = []
    for _ in range(samples):
        raw = llm.call([{"role": "user", "content": judge_prompt(run)}], response_model=RubricScore)
        outs.append(raw if isinstance(raw, RubricScore) else RubricScore.model_validate_json(str(raw)))
    scores = {c: round(sum(getattr(o, c) for o in outs) / len(outs), 2) for c in CRITERIA}
    spread = max(max(getattr(o, c) for o in outs) - min(getattr(o, c) for o in outs) for c in CRITERIA)
    return {
        **scores,
        "mean": round(sum(scores.values()) / len(scores), 2),
        "samples": samples,
        "max_spread": spread,
        "comment": outs[0].comment,
    }


def week_checks(run: WeekRun) -> dict:
    check = check_brief(run.brief, run.reports, run.week)
    return {
        "threat_levels_match": int(check["threat_levels_match"]),  # type: ignore[call-overload]
        "threat_levels": check["threat_levels"],
        "kolkata_first": bool(check["kolkata_first"]),
    }


def human_template(weeks: list[int]) -> dict:
    return {
        "_how": "Score each week's brief (briefs/week-NN.md) 1-5 per criterion before running the judge. "
        + " ".join(f"{k}: {v}" for k, v in CRITERIA.items()),
        **{str(w): {c: None for c in CRITERIA} for w in weeks},
    }


def human_scores() -> dict[int, dict[str, int]]:
    """Your filled-in scores (weeks with every criterion scored)."""
    if not HUMAN_SCORES.exists():
        return {}
    data = json.loads(HUMAN_SCORES.read_text())
    return {
        int(w): s
        for w, s in data.items()
        if not w.startswith("_") and all(isinstance(s.get(c), int) for c in CRITERIA)
    }


def agreement(judged: dict[int, dict], human: dict[int, dict]) -> dict | None:
    """Mean absolute difference, judge vs you, over the scores you both gave (0 = identical, 4 = opposite)."""
    pairs = [(judged[w][c], human[w][c]) for w in judged if w in human for c in CRITERIA]
    if not pairs:
        return None
    return {"pairs": len(pairs), "mean_abs_diff": round(sum(abs(a - b) for a, b in pairs) / len(pairs), 2)}


def scores(checks: dict[int, dict], judged: dict[int, dict] | None) -> dict[str, float | None]:
    from evals.signals import pct

    out: dict[str, float | None] = {
        "quality.threat_levels": pct(sum(c["threat_levels_match"] for c in checks.values()), 3 * len(checks)),
        "quality.week2_kolkata_first": (100.0 if checks[2]["kolkata_first"] else 0.0)
        if 2 in checks
        else None,
    }
    if judged:  # 1-5 mapped to 0-100, so the gate's 3-point rule reads the same for every metric
        mean = sum(j["mean"] for j in judged.values()) / len(judged)
        out["quality.judge"] = round((mean - 1) / 4 * 100, 1)
    return out
