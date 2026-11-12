"""Faithfulness and rumor handling (spec §17.1).

- ids (deterministic): every signal id a brief cites exists, in that week's reports or an earlier week's
  ("since last week" may cite last week's signals).
- cited (deterministic): the claims that must cite (TL;DR, threat rationales, recommendations) do.
- judge (Haiku, one batched call per week): does each claim follow from the signals it cites? supported = 1,
  partial = 0.5, unsupported = 0. Acceptance: >= 95%.
- rumor: no must-not-claim phrase outside discounted_claims (every week), and every rumor ground truth says
  must be discounted is listed there by id.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from compintel.models import Brief, Signal
from compintel.world.ground_truth import check_brief, find
from evals.data import WeekRun

SIGNAL_ID = re.compile(r"\b(?:StockSathi|ShelfMitra|BahiBook)-\d+-\d+\b")


@dataclass
class Claim:
    section: str
    text: str
    ids: list[str] = field(default_factory=list)
    must_cite: bool = True


def claims(brief: Brief) -> list[Claim]:
    """Every statement the brief makes as fact, with the ids it cites. Discounted claims are not claims."""

    def ids(*texts: str, extra: list[str] | None = None) -> list[str]:
        found = [i for t in texts for i in SIGNAL_ID.findall(t)] + list(extra or [])
        return list(dict.fromkeys(found))

    out = [Claim("tl_dr", t, ids(t)) for t in brief.tl_dr]
    out += [Claim("since_last_week", t, ids(t), must_cite=False) for t in brief.since_last_week]
    out += [
        Claim(
            "threat", f"{t.company} threat is {t.threat}: {t.rationale}", ids(t.rationale, extra=t.signal_ids)
        )
        for t in brief.threats
    ]
    out += [
        Claim("recommendation", r.action, ids(r.action, extra=r.signal_ids)) for r in brief.recommendations
    ]
    out += [Claim("watchlist", t, ids(t), must_cite=False) for t in brief.watchlist]
    return out


def all_cited(brief: Brief) -> list[str]:
    """Every id cited anywhere in the brief, discounted claims included (an id there must exist too)."""
    return SIGNAL_ID.findall(brief.model_dump_json())


def week_ids(run: WeekRun, known: dict[str, Signal]) -> dict:
    cited = all_cited(run.brief)
    unknown = sorted({i for i in cited if i not in known})
    must = [c for c in claims(run.brief) if c.must_cite]
    return {
        "ids_cited": len(cited),
        "ids_valid": sum(i in known for i in cited),
        "unknown_ids": unknown,
        "claims_must_cite": len(must),
        "claims_citing": sum(bool(c.ids) for c in must),
        "uncited": [f"{c.section}: {c.text[:80]}" for c in must if not c.ids],
    }


def week_rumor(run: WeekRun) -> dict:
    check = check_brief(run.brief, run.reports, run.week)
    discount = [find(e, run.reports) for e in run.truth["must_discount"]]
    listed = " ".join(run.brief.discounted_claims)
    return {
        "claims_rumor": bool(check["claims_rumor"]),
        "must_discount": len(discount),
        "discounted": sum(s is not None and s.id in listed for s in discount),
    }


# ---------------------------------------------------------------- the judge
class ClaimVerdict(BaseModel):
    n: int = Field(description="The claim's number in the list.")
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str = Field(description="One short sentence; name the detail that isn't supported, if any.")


class FaithfulnessVerdicts(BaseModel):
    verdicts: list[ClaimVerdict]


JUDGE_INSTRUCTIONS = """You check a competitive-intelligence brief written for KiranaCloud ("we", "our")
against its sources. Each numbered claim below lists the competitor signals it cites. Three more sources
are allowed: KiranaCloud's own facts (its pricing, roadmap, team); the signals of earlier weeks, for claims
about what changed or is new; and last week's published brief, only for claims about what that brief said
(its threat levels, what it reported). Judge only from these sources, not from what you know.

- supported: every fact in the claim (numbers, prices, places, names, direction of a change) is stated in or
  directly follows from the cited signals, KiranaCloud's facts, earlier weeks' signals, or (for what last
  week's brief said) that brief. A recommendation or threat level is supported when the sources give a
  reasonable basis for it.
- partial: mostly supported, but one detail goes beyond the sources.
- unsupported: a fact contradicts the sources or appears in none of them, or the claim states as fact
  something a signal with credibility below 0.5 only reports.

Return one verdict per claim, using its number."""

VERDICT_SCORE = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}
# Part of the judge's recorded name: a baseline judged with another prompt isn't gated against
# (scoring.comparable).
# v2 (M9): last week's published brief is a source; v1 flagged true "week 2 rated StockSathi high" claims.
PROMPT_VERSION = 2


def our_facts() -> str:
    """KiranaCloud's own facts: the knowledge base the crews read (markdown files; the PDF is a template)."""
    from compintel.config import get_settings

    files = sorted(get_settings().p3_knowledge_dir.glob("*.md"))
    return "\n\n".join(f.read_text().strip() for f in files)


def signal_line(s: Signal) -> str:
    fields = f"credibility {s.source_credibility}, impact {s.impact_on_us}, novelty {s.novelty}"
    return f"[{s.id}] ({fields}) {s.detail}"


def judge_prompt(
    items: list[tuple[int, Claim]], known: dict[str, Signal], week: int, context: str, previous: str = ""
) -> str:
    earlier = [s for s in known.values() if int(s.id.split("-")[1]) < week]
    lines = [JUDGE_INSTRUCTIONS, "", "## KiranaCloud's own facts", context or "(none)", ""]
    lines += ["## Earlier weeks' signals", *([signal_line(s) for s in earlier] or ["(none: week 1)"]), ""]
    lines += ["## Last week's published brief", previous.strip() or "(none: week 1)", ""]
    lines += ["## Claims", ""]
    for n, c in items:
        lines.append(f"Claim {n} ({c.section}): {c.text}")
        lines += [f"  - {signal_line(known[i])}" for i in c.ids if i in known]
        lines.append("")
    return "\n".join(lines)


def judge_week(
    run: WeekRun, known: dict[str, Signal], llm: Any, samples: int = 3, context: str = "", previous: str = ""
) -> dict:
    """`samples` calls over every claim that cites an existing signal (uncited claims are counted by `ids`);
    each claim scores the mean of its samples. Live M8c: one sample per run moved the week's score by up
    to 9 points between identical runs, more than the gate's 3."""
    items = [(n, c) for n, c in enumerate(claims(run.brief), 1) if any(i in known for i in c.ids)]
    if not items:
        return {"judged": 0, "points": 0.0, "samples": samples, "verdicts": []}
    prompt = judge_prompt(items, known, run.week, context, previous)
    rounds: list[dict[int, ClaimVerdict]] = []
    for _ in range(samples):
        raw = llm.call([{"role": "user", "content": prompt}], response_model=FaithfulnessVerdicts)
        out = (
            raw
            if isinstance(raw, FaithfulnessVerdicts)
            else FaithfulnessVerdicts.model_validate_json(str(raw))
        )
        rounds.append({v.n: v for v in out.verdicts})
    verdicts = []
    for n, c in items:
        got = [r.get(n) for r in rounds]
        # A claim a sample skipped isn't given the benefit of the doubt.
        labels = [v.verdict if v else "unsupported" for v in got]
        score = sum(VERDICT_SCORE[label] for label in labels) / len(labels)
        reasons = [v.reason if v else "no verdict returned" for v, lab in zip(got, labels, strict=True)
                   if lab != "supported"]  # fmt: skip
        verdicts.append(
            {
                "n": n,
                "section": c.section,
                "claim": c.text,
                "score": round(score, 3),
                "verdicts": labels,
                "verdict": Counter(labels).most_common(1)[0][0],  # the majority (ties: the first sample's)
                "reason": reasons[0] if reasons else "",
                "unanimous": len(set(labels)) == 1,
            }
        )
    return {
        "judged": len(verdicts),
        "points": round(sum(v["score"] for v in verdicts), 3),
        "samples": samples,
        "split": sum(not v["unanimous"] for v in verdicts),
        "verdicts": verdicts,
    }


def scores(
    ids: dict[int, dict], rumor: dict[int, dict], judged: dict[int, dict] | None
) -> dict[str, float | None]:
    from evals.signals import pct

    total = lambda d, key: sum(w[key] for w in d.values())  # noqa: E731
    out = {
        "faithfulness.ids": pct(total(ids, "ids_valid"), total(ids, "ids_cited")),
        "faithfulness.cited": pct(total(ids, "claims_citing"), total(ids, "claims_must_cite")),
        "rumor.never_claimed": pct(sum(not w["claims_rumor"] for w in rumor.values()), len(rumor)),
        "rumor.discounted": pct(total(rumor, "discounted"), total(rumor, "must_discount")),
    }
    if judged:
        judged_n = sum(w["judged"] for w in judged.values())
        points = sum(w.get("points", 0.0) for w in judged.values())
        out["faithfulness.judge"] = None if judged_n == 0 else round(100 * points / judged_n, 1)
    return out
