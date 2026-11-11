"""Signal recall and precision against ground truth, and routing (spec §17.1). Deterministic, $0.

- recall: of the signals ground truth expects (material_signals + must_surface + must_discount), how many a
  scout reported (`world.ground_truth.find`: one reported detail contains every expected substring).
- material recall: the same for material_signals only (week 2 has four; acceptance: 100%).
- kind accuracy: of the expected signals found, how many carry the expected kind.
- high-impact precision: of the signals scouts rated `high` impact, how many match a material or must-surface
  expectation. Ground truth names only the key signals, so plain precision (every reported signal matched
  to a listed one) would count every true but unlisted price or changelog line as a false positive; the
  `high` flag is what drives routing and the brief, so that is where a false positive costs.
- routing: the published route is the expected one.
"""

from __future__ import annotations

from compintel.models import Signal
from compintel.world.ground_truth import find
from evals.data import WeekRun

EXPECTED_GROUPS = ("material_signals", "must_surface", "must_discount")


def matches(signal: Signal, expected: dict) -> bool:
    """`world.ground_truth.find`'s test, for one signal."""
    detail = signal.detail.lower()
    return signal.company == expected["company"] and all(
        n.lower() in detail for n in expected["detail_contains"]
    )


def week_scores(run: WeekRun) -> dict:
    gt = run.truth
    expected = [(group, e) for group in EXPECTED_GROUPS for e in gt[group]]
    results = [(group, e, find(e, run.reports)) for group, e in expected]
    found = [(g, e, s) for g, e, s in results if s is not None]
    # Key = matches any material / must-surface expectation, not just the first match: a careers page is often
    # reported as several signals (one per role), all about the same Kolkata hiring push.
    key = [e for g in ("material_signals", "must_surface") for e in gt[g]]
    key_ids = {s.id for s in run.signals.values() if any(matches(s, e) for e in key)}
    high = [s for s in run.signals.values() if s.impact_on_us == "high"]
    material = [s for g, _, s in results if g == "material_signals"]
    return {
        "expected": len(expected),
        "found": len(found),
        "missed": [f"{e['company']}: {' + '.join(e['detail_contains'])}" for _, e, s in results if s is None],
        "material": len(material),
        "material_found": sum(s is not None for s in material),
        "kind_ok": sum(s.kind == e.get("kind", s.kind) for _, e, s in found),
        "high": len(high),
        "high_key": sum(s.id in key_ids for s in high),
        "high_extra": [f"{s.id}: {s.detail[:70]}" for s in high if s.id not in key_ids],
        "route": run.route,
        "route_ok": run.route == gt["expected_route"],
    }


def pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100 * numerator / denominator, 1)


def scores(per_week: dict[int, dict]) -> dict[str, float | None]:
    """0-100 metrics across weeks (None: nothing to measure)."""
    total = lambda key: sum(w[key] for w in per_week.values())  # noqa: E731
    w2 = per_week.get(2)
    return {
        "signals.recall": pct(total("found"), total("expected")),
        "signals.material_recall": pct(total("material_found"), total("material")),
        "signals.week2_material_recall": pct(w2["material_found"], w2["material"]) if w2 else None,
        "signals.kind_accuracy": pct(total("kind_ok"), total("found")),
        "signals.high_impact_precision": pct(total("high_key"), total("high")),
        "routing.accuracy": pct(sum(w["route_ok"] for w in per_week.values()), len(per_week)),
    }
