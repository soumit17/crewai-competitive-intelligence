"""M5 CompetitiveIntelFlow: routing over three weeks, the @persist fork, crash-resume, the plot. All $0.

Scouting and the two crews are stubbed (they have their own tests); the Flow, its router, @persist, the state
index and publishing are real.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from compintel import materiality
from compintel.flow import CompetitiveIntelFlow, read_state_index, run_week
from compintel.models import Brief, Signal, SignalReport, TieBreak
from tests.fakes import Rule, ScriptedLLM


def sig(sid, kind, impact, novelty="new", cred=0.9):
    return Signal(id=sid, company=sid.split("-")[0], kind=kind, detail=f"{kind} {sid}", source_ref="x",  # type: ignore[arg-type]
                  source_credibility=cred, novelty=novelty, impact_on_us=impact)  # type: ignore[arg-type]  # fmt: skip


def reports(week, *signals):
    by_company: dict[str, list[Signal]] = {"StockSathi": [], "ShelfMitra": [], "BahiBook": []}
    for s in signals:
        by_company[s.company].append(s)
    return {c: SignalReport(company=c, week=week, signals=ss, summary="") for c, ss in by_company.items()}  # type: ignore[arg-type]


# The labels the live scouts gave (docs/decisions.md, M5): week 3 has four "new" high-impact signals.
WEEKS = {
    1: reports(1, sig("StockSathi-1-1", "feature_launch", "low"), sig("BahiBook-1-1", "pricing_change",
    "medium")),
    2: reports(2, sig("StockSathi-2-1", "pricing_change", "high"), sig("ShelfMitra-2-1", "funding", "high"),
               sig("ShelfMitra-2-2", "hiring_expansion", "high")),
    3: reports(3, sig("StockSathi-3-1", "rating_change", "high"), sig("ShelfMitra-3-1", "feature_launch",
    "high"),
               sig("ShelfMitra-3-2", "other", "high"), sig("BahiBook-3-3", "hiring_expansion", "high"),
               sig("StockSathi-3-7", "rumor", "medium", cred=0.2)),
}  # fmt: skip
THREATS = {
    1: {"StockSathi": "low", "ShelfMitra": "low", "BahiBook": "low"},
    2: {"StockSathi": "high", "ShelfMitra": "high", "BahiBook": "low"},
    3: {"StockSathi": "medium", "ShelfMitra": "high", "BahiBook": "medium"},
}


def brief_for(week, route):
    return Brief(
        week=week,
        headline=f"Week {week}",
        tl_dr=[f"Something happened [StockSathi-{week}-1]"],
        since_last_week=["-"],
        threats=[
            {"company": c, "threat": t, "rationale": "x", "signal_ids": []} for c, t in THREATS[week].items()
        ],  # type: ignore[misc]
        recommendations=[],
        watchlist=[],
        route=route,
    )


@pytest.fixture
def stubs(monkeypatch):
    from compintel import scouting
    from compintel.crews import deep_dive, short_update

    calls: dict[str, list] = {"scout": [], "short_update": [], "deep_dive": [], "assess": []}
    week_reports = dict(WEEKS)

    async def scout_all(week, llm=None, verbose=False):
        calls["scout"].append(week)
        return week_reports[week]

    def run_short(week, reports, final=False, fake=None):
        calls["short_update"].append(week)
        return SimpleNamespace(brief=brief_for(week, "routine"))

    def run_deep(week, reports, variant="manager_agent", final=False, fake=None, verbose=False):
        calls["deep_dive"].append(week)
        return SimpleNamespace(brief=brief_for(week, "material"))

    real_assess = materiality.assess

    def counting_assess(*args, **kwargs):
        calls["assess"].append(1)
        return real_assess(*args, **kwargs)

    monkeypatch.setattr(scouting, "scout_all", scout_all)
    monkeypatch.setattr(short_update, "run", run_short)
    monkeypatch.setattr(deep_dive, "run", run_deep)
    monkeypatch.setattr(materiality, "assess", counting_assess)
    return SimpleNamespace(calls=calls, reports=week_reports)


def test_three_weeks_route_correctly_through_forks(world, stubs):
    flows = [run_week(w) for w in (1, 2, 3)]
    assert [f.state.route for f in flows] == ["routine", "material", "routine"]
    assert stubs.calls["short_update"] == [1, 3] and stubs.calls["deep_dive"] == [2]
    # Each week is its own execution (fork = new state.id) that carries the earlier weeks forward.
    assert len({f.state.id for f in flows}) == 3
    assert [len(f.state.history) for f in flows] == [0, 1, 2]
    assert flows[2].state.history[-1].threats["ShelfMitra"] == "high"  # what demoted week 3's continuations
    assert flows[2].state.previous_state_id == flows[1].state.id
    index = read_state_index()
    assert index["published"] == {str(w): f.state.id for w, f in zip((1, 2, 3), flows, strict=True)}
    assert index["in_progress"] == {}
    assert json.loads(Path("briefs/week-02.json").read_text())["route"] == "material"
    assert flows[0].state.our_dukaan_price == 799  # the parallel knowledge-digest @start


def test_router_runs_once_after_both_starts(world, stubs):
    run_week(1)
    assert stubs.calls["assess"] == [1]  # and_(scout_all, load_knowledge_digest): once, after both


def test_week_three_would_be_material_without_last_week():
    """Why the fork matters: without week 2's threat levels, week 3's continuations look like fresh moves."""
    assert materiality.assess(WEEKS[3], THREATS[2]).route == "routine"
    assert materiality.assess(WEEKS[3], {}).route == "material"


def test_a_fork_from_an_unknown_state_is_refused_not_silently_fresh(world, stubs):
    # crewai falls back to a fresh state when restore_from_state_id isn't found; load_week catches it.
    with pytest.raises(Exception, match="needs week 1's state"):
        CompetitiveIntelFlow().kickoff(inputs={"week": 2}, restore_from_state_id="no-such-state")


def test_week_two_before_week_one_is_refused(world, stubs):
    with pytest.raises(ValueError, match="no published state for week 1"):
        run_week(2)


def test_quiet_week_publishes_without_any_crew(world, stubs):
    stubs.reports[1] = reports(1, sig("BahiBook-1-1", "other", "low", novelty="repeat"))
    flow = run_week(1)
    assert flow.state.route == "nothing_new"
    assert stubs.calls["short_update"] == [] and stubs.calls["deep_dive"] == []
    assert flow.state.brief is not None and flow.state.brief.headline.startswith("Quiet week")
    assert Path("briefs/week-01.md").exists()


def test_a_tie_is_broken_by_one_llm_call(world, stubs, monkeypatch):
    import compintel.flow as flow_module

    stubs.reports[1] = reports(
        1, sig("StockSathi-1-1", "feature_launch", "high"), sig("BahiBook-1-1", "other", "high")
    )
    llm = ScriptedLLM(Rule(TieBreak(route="material", reason="Two strong moves at once.")))
    monkeypatch.setattr(flow_module, "make_llm", lambda **_: llm)
    flow = run_week(1)
    assert flow.state.route == "material" and stubs.calls["deep_dive"] == [1]
    assert len(llm.calls) == 1 and llm.calls[0]["response_model"] is TieBreak
    assert any("tie broken by LLM" in r for r in flow.state.materiality_reasons)


def test_resume_after_a_crash_does_not_pay_for_scouting_again(world, stubs, monkeypatch):
    from compintel.crews import deep_dive

    run_week(1)
    real_deep = deep_dive.run

    def crash_once(*args, **kwargs):
        monkeypatch.setattr(deep_dive, "run", real_deep)  # the next attempt works
        raise RuntimeError("process killed mid deep dive")

    monkeypatch.setattr(deep_dive, "run", crash_once)
    with pytest.raises(Exception, match="process killed"):
        run_week(2)
    crashed = read_state_index()["in_progress"]["2"]
    assert stubs.calls["scout"] == [1, 2]

    resumed = run_week(2, resume=True)
    assert stubs.calls["scout"] == [1, 2]  # week 2's reports came from the persisted state
    assert stubs.calls["deep_dive"] == [2] and resumed.state.route == "material"
    assert resumed.state.id != crashed  # a fork: the crashed run stays on record
    assert (
        read_state_index()["published"]["2"] == resumed.state.id and read_state_index()["in_progress"] == {}
    )
    assert run_week(3).state.route == "routine"  # and the week after forks the resumed run normally


def test_nothing_to_resume_is_an_error(world, stubs):
    with pytest.raises(ValueError, match="no unfinished run of week 1"):
        run_week(1, resume=True)


def test_plot_draws_every_router_branch(tmp_path):
    html = Path(CompetitiveIntelFlow().plot("flow.html", show=False))
    script = (html.parent / "flow_script.js").read_text()
    for label in ("nothing_new", "routine", "material", "load_knowledge_digest", "run_deep_dive", "publish"):
        assert label in script
