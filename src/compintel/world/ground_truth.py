"""Deterministic checks of scout output against data/ground_truth (spec §4.4). The M2 acceptance check; the M8
eval harness builds on it.

An expected signal is found when one reported signal for that company contains every `detail_contains`
substring (case-insensitive) in its detail. `kind` is reported separately: a right fact under a debatable
kind still counts as found.
"""

from __future__ import annotations

from dataclasses import dataclass

from compintel.models import Brief, Signal, SignalReport
from compintel.world.story import GROUND_TRUTH


@dataclass
class Match:
    expected: dict
    signal: Signal | None

    @property
    def found(self) -> bool:
        return self.signal is not None

    @property
    def kind_ok(self) -> bool:
        return self.signal is not None and self.signal.kind == self.expected.get("kind", self.signal.kind)


def find(expected: dict, reports: dict[str, SignalReport]) -> Signal | None:
    report = reports.get(expected["company"])
    for sig in report.signals if report else []:
        if all(needle.lower() in sig.detail.lower() for needle in expected["detail_contains"]):
            return sig
    return None


def check_week(reports: dict[str, SignalReport], week: int) -> dict[str, list[Match]]:
    gt = GROUND_TRUTH[week]
    return {
        group: [Match(e, find(e, reports)) for e in gt[group]]
        for group in ("material_signals", "must_surface", "must_discount")
    }


def check_brief(brief: Brief, reports: dict[str, SignalReport], week: int) -> dict[str, object]:
    """Deterministic quality checks of a brief against ground truth (the M8 eval harness extends these).

    - material_cited: of the week's material signals the scouts found, how many the brief cites by id
    - kolkata_first: the first TL;DR item is about the Kolkata threat (spec §17.1: "Kolkata threat leads")
    - threat_levels: per competitor, the brief's level vs the expected one
    - claims_rumor: a must-not-claim phrase appears outside discounted_claims
    """
    gt = GROUND_TRUTH[week]
    text = brief.model_dump_json()
    found = [find(e, reports) for e in gt["material_signals"]]
    cited = [s for s in found if s is not None and s.id in text]
    outside = brief.model_dump_json(exclude={"discounted_claims"}).lower()
    levels = {t.company: t.threat for t in brief.threats}
    return {
        "material_cited": f"{len(cited)}/{len(gt['material_signals'])}",
        "kolkata_first": bool(brief.tl_dr) and "kolkata" in brief.tl_dr[0].lower(),
        "threat_levels": {c: f"{levels.get(c)} (expected {lvl})" for c, lvl in gt["expected_threat"].items()},
        "threat_levels_match": sum(levels.get(c) == lvl for c, lvl in gt["expected_threat"].items()),
        "claims_rumor": any(p.lower() in outside for p in gt["must_not_claim"]),
    }
