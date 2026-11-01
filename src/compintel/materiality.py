"""How material is this week? Deterministic first; an LLM only breaks ties (spec §9.1: routers should be
mostly code).

The scouts have no memory: "new" in a SignalReport means "on this week's pages". The Flow knows last week (its
state is forked from last week's run), so it can tell a fresh escalation from the continuation of a story it
already escalated: a competitor already rated "high" last week isn't escalating again because it took the
next step.

Routes:
- nothing_new: no new, credible signal at all
- material:    a fresh decisive move (pricing change, funding), or MATERIAL_AT+ fresh high-impact signals
- tie:         exactly TIE_AT fresh high-impact signals -> one LLM call decides routine/material
- routine:     anything else
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from compintel.models import Signal, SignalReport

CREDIBLE = 0.5
DECISIVE_KINDS = {"pricing_change", "funding"}
TIE_AT = 2
MATERIAL_AT = 3
IMPACT_WEIGHT = {"none": 0.0, "low": 0.1, "medium": 0.3, "high": 1.0}


@dataclass
class Assessment:
    route: Literal["nothing_new", "routine", "material", "tie"]
    # Sum of impact weight x credibility over the fresh signals: for display, not for the decision.
    score: float
    # Ids of fresh, credible, high-impact signals.
    fresh_high: list[str] = field(default_factory=list)
    # High-impact, but continuing a competitor rated "high" last week.
    continuations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def assess(reports: dict[str, SignalReport], previous_threats: dict[str, str] | None = None) -> Assessment:
    previous_threats = previous_threats or {}
    new: list[Signal] = [
        s
        for r in reports.values()
        for s in r.signals
        if s.novelty == "new" and s.source_credibility >= CREDIBLE
    ]
    if not new:
        return Assessment("nothing_new", 0.0, reasons=["no new, credible signals"])

    fresh = [s for s in new if previous_threats.get(s.company) != "high"]
    continuations = [s.id for s in new if s not in fresh and s.impact_on_us == "high"]
    fresh_high = [s for s in fresh if s.impact_on_us == "high"]
    decisive = [s for s in fresh_high if s.kind in DECISIVE_KINDS]
    score = round(sum(IMPACT_WEIGHT[s.impact_on_us] * s.source_credibility for s in fresh), 2)
    ids = [s.id for s in fresh_high]
    reasons = [
        f"{len(fresh_high)} fresh high-impact signal(s)",
        f"{len(continuations)} continuation(s) of last week's high threats",
    ]

    if decisive:
        route: Literal["nothing_new", "routine", "material", "tie"] = "material"
        reasons.append(f"decisive move(s): {', '.join(s.id for s in decisive)}")
    elif len(fresh_high) >= MATERIAL_AT:
        route = "material"
    elif len(fresh_high) == TIE_AT:
        route = "tie"
    else:
        route = "routine"
    return Assessment(route, score, ids, continuations, reasons)
