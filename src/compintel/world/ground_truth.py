"""Deterministic checks of scout output against data/ground_truth (spec §4.4). The M2 acceptance check; the M8
eval harness builds on it.

An expected signal is found when one reported signal for that company contains every `detail_contains`
substring (case-insensitive) in its detail. `kind` is reported separately: a right fact under a debatable
kind still counts as found.
"""

from __future__ import annotations

from dataclasses import dataclass

from compintel.models import Signal, SignalReport
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
