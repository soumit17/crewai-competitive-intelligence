"""M8c eval harness (evals/), $0: hand-built weeks with known faults, scripted judges, and one pass through
the real loader on Flow-published weeks."""

from __future__ import annotations

import json

import pytest

from compintel.models import Brief, Recommendation, Signal, SignalReport, ThreatAssessment
from evals import faithfulness, quality, scoring, signals
from evals.data import WeekRun, load_weeks
from tests.fakes import Rule, ScriptedLLM
from tests.test_flow import run_week
from tests.test_flow import stubs as stubs  # noqa: F401  (fixture)


def sig(sid: str, detail: str, impact: str = "medium", kind: str = "other", cred: float = 0.9) -> Signal:
    return Signal(id=sid, company=sid.split("-")[0], kind=kind, detail=detail, source_ref="x",  # type: ignore[arg-type]
                  source_credibility=cred, novelty="new", impact_on_us=impact)  # type: ignore[arg-type]  # fmt: skip


def reports(week: int, *signals_: Signal) -> dict[str, SignalReport]:
    out: dict[str, list[Signal]] = {"StockSathi": [], "ShelfMitra": [], "BahiBook": []}
    for s in signals_:
        out[s.company].append(s)
    return {c: SignalReport(company=c, week=week, signals=ss, summary="") for c, ss in out.items()}  # type: ignore[arg-type]


WEEK2 = reports(
    2,
    sig("StockSathi-2-1", "Pro cut 30% to ₹699 per month", "high", "pricing_change"),
    sig("StockSathi-2-2", "UPI Udhaar: credit tracking over UPI", "high", "feature_launch"),
    sig("ShelfMitra-2-1", "Series B of ₹200 crore", "high", "funding"),
    sig("ShelfMitra-2-2", "15 sales roles in Kolkata", "high", "hiring_expansion"),
    sig("ShelfMitra-2-3", "2 more Territory Sales roles in Kolkata", "high", "hiring_expansion"),  # same push
    sig("BahiBook-2-1", "A blog post about GST", "high"),  # not a key signal: a false positive
)


def brief(week: int = 2, **changes) -> Brief:
    base = dict(
        week=week,
        headline="Kolkata threat rises",
        tl_dr=["ShelfMitra raised ₹200 crore and hires 15 in Kolkata [ShelfMitra-2-1][ShelfMitra-2-2]"],
        since_last_week=["StockSathi cut Pro to ₹699 [StockSathi-2-1]"],
        threats=[
            ThreatAssessment(company="StockSathi", threat="high", rationale="Price cut [StockSathi-2-1]",
                             signal_ids=["StockSathi-2-1"]),
            ThreatAssessment(company="ShelfMitra", threat="high", rationale="Funded [ShelfMitra-2-1]",
                             signal_ids=["ShelfMitra-2-1"]),
            ThreatAssessment(company="BahiBook", threat="low", rationale="Only a blog post [BahiBook-2-1]",
                             signal_ids=["BahiBook-2-1"]),
        ],
        recommendations=[Recommendation(action="Defend Kolkata accounts", owner="Sales", urgency="this_week",
                                        signal_ids=["ShelfMitra-2-2"])],
        watchlist=[],
        discounted_claims=[],
        route="material",
    )  # fmt: skip
    return Brief(**{**base, **changes})  # type: ignore[arg-type]


def run(week: int = 2, reps: dict | None = None, b: Brief | None = None, route: str = "material") -> WeekRun:
    return WeekRun(week, f"state-{week}", reps if reps is not None else WEEK2, b or brief(week), route)


# ---------------------------------------------------------------- signals and routing
def test_recall_precision_and_routing_on_a_good_week_two():
    s = signals.week_scores(run())
    assert (s["found"], s["expected"], s["material_found"], s["kind_ok"]) == (4, 4, 4, 4)
    # Six high-impact signals: five match an expectation (the second Kolkata hiring line too), one doesn't.
    assert (s["high"], s["high_key"]) == (6, 5) and s["high_extra"][0].startswith("BahiBook-2-1")
    assert s["route_ok"]


def test_a_missed_signal_and_a_wrong_route_show():
    reps = reports(2, *[x for r in WEEK2.values() for x in r.signals if x.id != "StockSathi-2-2"])
    s = signals.week_scores(run(reps=reps, route="routine"))
    assert s["material_found"] == 3 and s["missed"] == ["StockSathi: UPI + credit"]
    assert not s["route_ok"]
    assert signals.scores({2: s})["signals.week2_material_recall"] == 75.0


# ---------------------------------------------------------------- faithfulness (deterministic) and rumor
def test_every_cited_id_must_exist_and_key_claims_must_cite():
    bad = brief(tl_dr=["Big news [ShelfMitra-2-9]", "StockSathi is cheaper now"])
    ids = faithfulness.week_ids(run(b=bad), run().signals)
    assert ids["unknown_ids"] == ["ShelfMitra-2-9"]
    assert ids["uncited"] == ["tl_dr: StockSathi is cheaper now"]
    assert ids["claims_citing"] == ids["claims_must_cite"] - 1
    assert faithfulness.week_ids(run(), run().signals)["uncited"] == []  # the good brief cites everywhere


def test_last_weeks_ids_are_valid_in_since_last_week():
    known = {**run().signals, "StockSathi-1-1": sig("StockSathi-1-1", "Basic at ₹499")}
    b = brief(since_last_week=["Basic still ₹499 [StockSathi-1-1]"])
    assert faithfulness.week_ids(run(b=b), known)["unknown_ids"] == []


def test_a_rumor_stated_as_fact_and_a_rumor_left_undiscounted_both_fail():
    reps = reports(3, sig("StockSathi-3-8", "Unverified report: StockSathi acquired by a bank", "medium",
                          "rumor", cred=0.2))  # fmt: skip
    claimed = brief(week=3, tl_dr=["StockSathi acquired by a bank [StockSathi-3-8]"])
    r = faithfulness.week_rumor(run(3, reps, claimed, "routine"))
    assert r["claims_rumor"] and (r["must_discount"], r["discounted"]) == (1, 0)
    handled = brief(week=3, discounted_claims=["Unverified: StockSathi acquired [StockSathi-3-8]"])
    r = faithfulness.week_rumor(run(3, reps, handled, "routine"))
    assert not r["claims_rumor"] and r["discounted"] == 1


# ---------------------------------------------------------------- the judges (scripted)
def faith_judge(*verdicts: str) -> ScriptedLLM:
    def answer(messages, response_model):
        prompt = messages[-1]["content"]
        assert "[StockSathi-2-1] (credibility 0.9) Pro cut 30% to ₹699 per month" in prompt
        n = prompt.count("Claim ")
        return {"verdicts": [{"n": i, "verdict": verdicts[(i - 1) % len(verdicts)], "reason": "r"}
                             for i in range(1, n + 1)]}  # fmt: skip

    return ScriptedLLM(Rule(answer))


def test_faithfulness_judge_scores_supported_partial_and_unsupported_in_one_call():
    llm = faith_judge("supported", "partial", "unsupported", "supported")
    j = faithfulness.judge_week(run(), run().signals, llm)
    assert len(llm.calls) == 1 and llm.calls[0]["response_model"] is faithfulness.FaithfulnessVerdicts
    # Claims citing an existing signal: tl_dr, since_last_week, 3 threats, 1 recommendation.
    assert j["judged"] == 6 and j["points"] == 1 + 0.5 + 0 + 1 + 1 + 0.5
    assert [v["verdict"] for v in j["verdicts"]][:3] == ["supported", "partial", "unsupported"]


def test_a_claim_the_judge_skips_counts_as_unsupported():
    llm = ScriptedLLM(Rule(lambda m, rm: {"verdicts": [{"n": 1, "verdict": "supported", "reason": "ok"}]}))
    j = faithfulness.judge_week(run(), run().signals, llm)
    assert j["points"] == 1 and sum(v["verdict"] == "unsupported" for v in j["verdicts"]) == 5


def quality_judge(score: int = 4) -> ScriptedLLM:
    return ScriptedLLM(Rule(lambda m, rm: {c: score for c in quality.CRITERIA} | {"comment": "Clear."}))


def test_quality_judge_sees_the_rendered_brief_and_maps_to_0_100():
    llm = quality_judge(4)
    q = quality.judge_week(run(), llm)
    assert q["mean"] == 4 and "# Week 02: Kolkata threat rises" in llm.calls[0]["messages"][-1]["content"]
    assert quality.scores({2: quality.week_checks(run())}, {2: q})["quality.judge"] == 75.0


# ---------------------------------------------------------------- scoring, baseline, gate, acceptance
def test_regressions_beyond_three_points_fail_the_gate_and_unmeasured_metrics_are_skipped():
    baseline = {"a": 90.0, "b": 90.0, "c": 50.0, "d": None}
    now = {"a": 87.0, "b": 86.9, "c": None, "d": 10.0}
    assert scoring.regressions(now, baseline) == ["b: 86.9 < baseline 90.0 - 3.0"]


def test_acceptance_needs_the_judge_to_have_run():
    ok = {"signals.week2_material_recall": 100.0, "rumor.never_claimed": 100.0, "faithfulness.judge": 96.0}
    assert scoring.acceptance(ok) == []
    assert scoring.acceptance({**ok, "faithfulness.judge": None}) == [
        "faithfulness.judge: not measured (needs >= 95.0)"
    ]


def test_human_scores_are_read_only_when_complete_and_compared_with_the_judge(tmp_path, monkeypatch):
    path = tmp_path / "human_scores.json"
    monkeypatch.setattr(quality, "HUMAN_SCORES", path)
    template = quality.human_template([1, 2])
    template["2"] = {c: 3 for c in quality.CRITERIA}  # week 1 left blank: ignored
    path.write_text(json.dumps(template))
    human = quality.human_scores()
    assert list(human) == [2]
    judged = {2: {c: 4 for c in quality.CRITERIA}}
    assert quality.agreement(judged, human) == {"pairs": 4, "mean_abs_diff": 1.0}


def test_the_whole_harness_on_flow_published_weeks(world, stubs, tmp_path, monkeypatch):
    """The real loader: published states from runs/state_index.json, judges scripted, report written."""
    monkeypatch.setattr(scoring, "REPORT", tmp_path / "report.md")
    monkeypatch.setattr(scoring, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(quality, "HUMAN_SCORES", tmp_path / "none.json")
    for week in (1, 2, 3):
        run_week(week)
    runs = load_weeks()
    assert [r.week for r in runs] == [1, 2, 3] and all(r.brief.week == r.week for r in runs)
    result = scoring.evaluate(runs, faith_llm=faith_judge_any(), quality_llm=quality_judge(5))
    s = result["scores"]
    assert s["routing.accuracy"] == 100.0  # routine, material, routine
    assert s["faithfulness.judge"] == 100.0 and s["quality.judge"] == 100.0
    assert s["rumor.never_claimed"] == 100.0
    scoring.save_baseline(result, {"faithfulness": "fake", "quality": "fake"})
    assert scoring.load_baseline()["scores"] == s
    same = scoring.comparable(scoring.load_baseline(), {"faithfulness": "fake", "quality": "fake"})
    other = scoring.comparable(scoring.load_baseline(), {"faithfulness": "fake", "quality": "sonnet"})
    assert "quality.judge" in same and "quality.judge" not in other and "faithfulness.judge" in other
    text = scoring.markdown(result, {}, same)
    assert "## Week 2 (route material)" in text and "## Process comparison" in text


def faith_judge_any() -> ScriptedLLM:
    def answer(messages, response_model):
        n = messages[-1]["content"].count("Claim ")
        return {"verdicts": [{"n": i, "verdict": "supported", "reason": "ok"} for i in range(1, n + 1)]}

    return ScriptedLLM(Rule(answer))


def test_an_unpublished_week_is_refused(world):
    with pytest.raises(ValueError, match="week 1 isn't published"):
        load_weeks()
