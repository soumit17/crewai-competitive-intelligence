"""M3 function guardrails, called directly with TaskOutputs."""

import json

import pytest
from crewai import TaskOutput

from compintel import guardrails
from compintel.models import Brief

GOOD = {
    "week": 2,
    "headline": "StockSathi undercuts us",
    "tl_dr": ["StockSathi Pro now ₹699 [StockSathi-2-1]"],
    "since_last_week": ["First brief: no earlier week to compare."],
    "threats": [
        {"company": "StockSathi", "threat": "high", "rationale": "Price cut [StockSathi-2-1]", "signal_ids": ["StockSathi-2-1"]}
    ],
    "recommendations": [
        {"action": "Review Dukaan pricing", "owner": "Leadership", "urgency": "this_week", "signal_ids": ["StockSathi-2-1"]}
    ],
    "watchlist": ["BahiBook hiring [BahiBook-1-3]"],
    "discounted_claims": [],
    "route": "routine",
}  # fmt: skip


def out(brief: dict | str) -> TaskOutput:
    raw = brief if isinstance(brief, str) else json.dumps(brief)
    return TaskOutput(description="d", raw=raw, agent="Brief Writer")


def variant(**changes) -> dict:
    return {**json.loads(json.dumps(GOOD)), **changes}


def test_good_brief_passes_and_comes_back_parsed():
    ok, value = guardrails.every_claim_cites_signal(out(GOOD))
    assert ok and isinstance(value, TaskOutput)
    assert isinstance(value.pydantic, Brief)  # so crewai needs no Converter call afterwards


def test_code_fenced_json_is_tolerated():
    ok, _ = guardrails.every_claim_cites_signal(out("```json\n" + json.dumps(GOOD) + "\n```"))
    assert ok


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tl_dr": ["StockSathi cut prices"]}, "tl_dr[0] cites no signal id"),
        ({"discounted_claims": ["StockSathi may be acquired"]}, "discounted_claims[0] cites no signal id"),
        (
            {"recommendations": [{"action": "x", "owner": "Sales", "urgency": "watch", "signal_ids": []}]},
            "recommendations[0] has no signal_ids",
        ),
        ({"tl_dr": ["Next week's news [ShelfMitra-3-1]"]}, "ShelfMitra-3-1 is from a future week"),
    ],
)
def test_citation_problems_are_reported_back_to_the_agent(changes, message):
    ok, error = guardrails.every_claim_cites_signal(out(variant(**changes)))
    assert not ok and message in error


def test_invented_ids_are_rejected_once_the_runs_signals_are_registered():
    guardrails.register_signals({"signals": json.dumps([{"id": "StockSathi-2-1"}])})
    assert guardrails.every_claim_cites_signal(out(GOOD))[0]
    bad = variant(tl_dr=["Made up [StockSathi-2-9]"])
    ok, error = guardrails.every_claim_cites_signal(out(bad))
    assert not ok and "StockSathi-2-9 is not one of this week's signals" in error
    # Ids from earlier weeks aren't in this run's inputs; they're allowed (since_last_week, watchlist).
    assert "BahiBook-1-3" in json.dumps(GOOD)


def test_not_a_brief_is_a_retryable_failure():
    ok, error = guardrails.every_claim_cites_signal(out('{"headline": "only this"}'))
    assert not ok and "not a valid Brief" in error


def test_brief_length_guardrail():
    assert guardrails.brief_length_ok(out(GOOD))[0]
    long = variant(watchlist=["word " * 700 + "[BahiBook-1-3]"])
    ok, error = guardrails.brief_length_ok(out(long))
    assert not ok and "at most 600" in error


def test_low_credibility_signals_belong_only_in_discounted_claims():
    guardrails.register_signals({"signals": json.dumps([
        {"id": "StockSathi-3-1", "credibility": 0.9},
        {"id": "StockSathi-3-2", "credibility": 0.2},
    ])})  # fmt: skip
    ok_brief = variant(week=3, tl_dr=["Rating fell [StockSathi-3-1]"], threats=[], recommendations=[],
                       discounted_claims=["Unverified acquisition blog [StockSathi-3-2]"])  # fmt: skip
    assert guardrails.low_credibility_only_discounted(out(ok_brief))[0]

    missing = variant(**{**ok_brief, "discounted_claims": []})
    ok, error = guardrails.low_credibility_only_discounted(out(missing))
    assert not ok and "StockSathi-3-2 (credibility < 0.5) is missing from discounted_claims" in error

    as_fact = variant(**{**ok_brief, "tl_dr": ["StockSathi acquired [StockSathi-3-2]"]})
    ok, error = guardrails.low_credibility_only_discounted(out(as_fact))
    assert not ok and "is used as fact outside discounted_claims" in error


def test_no_low_credibility_signals_means_nothing_to_discount():
    guardrails.register_signals({"signals": json.dumps([{"id": "StockSathi-2-1", "credibility": 0.9}])})
    assert guardrails.low_credibility_only_discounted(out(GOOD))[0]


def _register(*signals):
    guardrails.register_signals({"signals": json.dumps(list(signals))})


THREATS = {"threats": [
    {"company": "StockSathi", "threat": "high", "rationale": "x", "signal_ids": ["StockSathi-2-1"]},
    {"company": "ShelfMitra", "threat": "medium", "rationale": "x", "signal_ids": ["ShelfMitra-2-1"]},
    {"company": "BahiBook", "threat": "low", "rationale": "x", "signal_ids": ["BahiBook-2-1"]},
]}  # fmt: skip


def test_threat_levels_must_match_their_evidence():
    _register(
        {"id": "StockSathi-2-1", "impact": "high", "credibility": 0.9, "novelty": "new"},
        {"id": "ShelfMitra-2-1", "impact": "high", "credibility": 0.9, "novelty": "new"},
        {"id": "BahiBook-2-1", "impact": "none", "credibility": 0.9, "novelty": "new"},
    )
    ok, value = guardrails.threat_levels_consistent(out(THREATS))
    assert ok and value.pydantic.threats[0].threat == "high"

    weak_high = json.loads(json.dumps(THREATS))
    weak_high["threats"][2]["threat"] = "high"  # BahiBook "high" on a bug fix
    ok, error = guardrails.threat_levels_consistent(out(weak_high))
    assert not ok and "BahiBook is 'high' but cites no credible high-impact signal" in error

    ignored = json.loads(json.dumps(THREATS))
    ignored["threats"][1]["threat"] = "low"  # ShelfMitra "low" despite a new high-impact signal
    ok, error = guardrails.threat_levels_consistent(out(ignored))
    assert not ok and "ShelfMitra is 'low' but cites a new, credible, high-impact signal" in error


def test_threat_assessments_cover_each_competitor_once():
    two = {"threats": THREATS["threats"][:2]}
    ok, error = guardrails.threat_levels_consistent(out(two))
    assert not ok and "exactly one assessment each" in error


def test_rumor_cannot_make_a_threat_high():
    _register({"id": "StockSathi-3-7", "impact": "high", "credibility": 0.2, "novelty": "new"})
    rumor = {
        "threats": [{**THREATS["threats"][0], "signal_ids": ["StockSathi-3-7"]}, *THREATS["threats"][1:]]
    }
    _register(
        {"id": "StockSathi-3-7", "impact": "high", "credibility": 0.2, "novelty": "new"},
        {"id": "ShelfMitra-2-1", "impact": "medium", "credibility": 0.9, "novelty": "new"},
        {"id": "BahiBook-2-1", "impact": "none", "credibility": 0.9, "novelty": "new"},
    )
    ok, error = guardrails.threat_levels_consistent(out(rumor))
    assert not ok and "StockSathi is 'high'" in error


def test_escalation_condition_reads_the_synthesis():
    def synth(level):
        return out(
            {
                "threats": [{**THREATS["threats"][1], "threat": level}],
                "key_findings": [],
                "recommendations": [],
            }
        )

    assert guardrails.any_high_threat(synth("high"))
    assert not guardrails.any_high_threat(synth("medium"))
    assert not guardrails.any_high_threat(out("not json"))


def test_the_rule_text_is_the_same_in_both_crews():
    from compintel.crews import short_update

    assert json.dumps(guardrails.NO_RUMOR_AS_FACT)[1:-1] in short_update.CREW_FILE.read_text()
