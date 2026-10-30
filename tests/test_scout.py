"""M2 scouts with ScriptedLLM: every tool really runs (fake web, news DB, MCP subprocess over stdio); only the
model's decisions are scripted."""

import asyncio
import json
import re

import pytest

from compintel import hooks, scouting
from compintel.models import Signal, SignalReport
from compintel.world.ground_truth import check_week
from tests.fakes import Rule, ScriptedLLM, ToolCall, tool_results


def all_tools(company: str, week: int, careers_url: str) -> list[ToolCall]:
    args = {"company": company, "week": week}
    return [
        ToolCall("pricing_parse", args),
        ToolCall("changelog_diff", args),
        ToolCall("news_search", args),
        ToolCall("read_website_content", {"website_url": careers_url}),
        ToolCall("market_signals_funding_rounds", {"company": company}),
        ToolCall("market_signals_app_rating_trend", {"company": company, "weeks": 3}),
    ]


class ScriptedScout:
    """Scout decisions: call every tool once, then answer. Keeps what the tools returned for assertions."""

    def __init__(self, base_url: str, week: int):
        self.base_url, self.week = base_url, week
        self.tool_outputs: dict[str, list[dict]] = {}

    def __call__(self, messages, response_model):
        prompt = json.dumps(messages, default=str)
        # The conversion prompt embeds the schema (whose enum lists every company), so match exact phrases.
        found = re.search(r"Scout (\w+) for week|(\w+) scouted\.", prompt)
        company = next(g for g in found.groups() if g)  # type: ignore[union-attr]
        if response_model is SignalReport:  # the final conversion call
            return {"company": company, "week": self.week, "summary": "scripted", "signals": []}
        results = tool_results(messages)
        if '"role": "tool"' not in results:
            slug = company.lower()
            return all_tools(company, self.week, f"{self.base_url}/week-{self.week}/{slug}/careers.html")
        self.tool_outputs[company] = json.loads(results)
        return f"Final Answer: {company} scouted."


@pytest.fixture
def scout_llm(world):
    hooks.install()
    script = ScriptedScout(world, week=2)
    return script, ScriptedLLM(Rule(script))


def test_scout_sees_all_six_tools_including_mcp(scout_llm):
    script, llm = scout_llm
    asyncio.run(scouting.scout_company("ShelfMitra", 2, llm=llm))
    assert set(llm.calls[0]["tools"]) == {
        "news_search",
        "read_website_content",
        "pricing_parse",
        "changelog_diff",
        "market_signals_funding_rounds",
        "market_signals_app_rating_trend",
    }


def test_every_tool_really_runs_and_mcp_respects_the_week(scout_llm):
    script, llm = scout_llm
    report = asyncio.run(scouting.scout_company("ShelfMitra", 2, llm=llm))
    assert isinstance(report, SignalReport) and report.company == "ShelfMitra"
    out = {m["name"]: m["content"] for m in script.tool_outputs["ShelfMitra"]}
    assert out["read_website_content"].count("Kolkata") >= 3  # the 15 sales roles, scraped for real
    assert "Series B" in out["market_signals_funding_rounds"]  # MCP subprocess started with week 2
    assert "Bulk GST invoice export" in out["changelog_diff"]
    assert "news-w2-3" in out["news_search"]


def test_week_one_scout_cannot_see_the_week_two_series_b(world):
    hooks.install()
    script = ScriptedScout(world, week=1)
    asyncio.run(scouting.scout_company("ShelfMitra", 1, llm=ScriptedLLM(Rule(script))))
    out = {m["name"]: m["content"] for m in script.tool_outputs["ShelfMitra"]}
    assert "Series A" in out["market_signals_funding_rounds"]
    assert "Series B" not in out["market_signals_funding_rounds"]


def test_scout_all_fans_out_to_three_companies(scout_llm):
    script, llm = scout_llm
    reports = asyncio.run(scouting.scout_all(2, llm=llm))
    assert set(reports) == {"StockSathi", "ShelfMitra", "BahiBook"}
    assert set(script.tool_outputs) == set(reports)


def test_kickoff_for_each_alternative_gives_the_same_shape(scout_llm):
    script, llm = scout_llm
    reports = scouting.scout_all_crew(2, llm=llm)
    assert set(reports) == {"StockSathi", "ShelfMitra", "BahiBook"}
    assert all(isinstance(r, SignalReport) for r in reports.values())


def _sig(company, n, kind, detail):
    return Signal(
        id=f"{company}-2-{n}",
        company=company,
        kind=kind,
        detail=detail,
        source_ref="x",
        source_credibility=0.9,
        novelty="new",
        impact_on_us="high",
    )


def test_ground_truth_check_finds_signals_by_detail_and_flags_kind():
    reports = {
        "StockSathi": SignalReport(
            company="StockSathi",
            week=2,
            summary="",
            signals=[
                _sig("StockSathi", 1, "pricing_change", "Pro cut 30% from ₹999 to ₹699/month"),
                _sig("StockSathi", 2, "other", "Launched UPI Udhaar store credit"),
            ],
        ),
        "ShelfMitra": SignalReport(company="ShelfMitra", week=2, summary="", signals=[]),
    }
    material = check_week(reports, 2)["material_signals"]
    assert [m.found for m in material] == [True, True, False, False]
    assert material[0].kind_ok and not material[1].kind_ok  # found, but filed under "other"


def test_json_final_answer_skips_the_conversion_call(world):
    """Agent.kickoff tries response_format.model_validate_json(final answer) first and only calls the LLM
    Converter when that fails. The scout prompt asks for bare JSON to save that call."""
    hooks.install()
    report = {"company": "BahiBook", "week": 2, "summary": "quiet", "signals": []}

    def step(messages, response_model):
        assert response_model is None, "conversion call made"
        if '"role": "tool"' not in tool_results(messages):
            return [ToolCall("changelog_diff", {"company": "BahiBook", "week": 2})]
        return json.dumps(report)

    llm = ScriptedLLM(Rule(step))
    out = asyncio.run(scouting.scout_company("BahiBook", 2, llm=llm))
    assert out.summary == "quiet"
    assert len(llm.calls) == 2  # tool round + final answer, no third (conversion) call


def test_crewai_agent_copy_drops_mcps():
    """crewai 1.15.33: Agent.copy() excludes `mcps` and doesn't restore them, and Crew.kickoff_for_each copies
    the crew per input, so the crew-mode scouts lost the market-signals server in the live M2 comparison.
    If this starts failing, upstream fixed it: revisit scout_all_crew and docs/decisions.md."""
    hooks.install()
    scout = scouting.make_scout(2, llm=ScriptedLLM())
    assert scout.mcps
    assert scout.copy().mcps is None
    assert {t.name for t in scout.copy().tools or []} == {t.name for t in scout.tools or []}  # tools survive
