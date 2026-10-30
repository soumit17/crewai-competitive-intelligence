"""M2 tools called directly (no LLM): exact parsing, week-over-week diffs, errors as strings, caching."""

import json

from compintel.tools.changelog_diff import changelog_diff
from compintel.tools.news_search import NewsSearchTool, cache_only_hits
from compintel.tools.pricing_parse import pricing_parse


def test_pricing_parse_reads_exact_prices_and_the_week_two_cut(world):
    out = json.loads(pricing_parse.run(company="StockSathi", week=2))
    pro = next(p for p in out["plans"] if p["plan"] == "Pro")
    assert pro["price_inr_per_month"] == 699 and pro["previous_price_inr"] == 999
    assert out["changes_since_last_week"] == ["Pro: ₹999 → ₹699/month (-30%)"]
    assert json.loads(pricing_parse.run(company="StockSathi", week=3))["changes_since_last_week"] == []


def test_pricing_parse_handles_contact_sales_plans(world):
    plans = json.loads(pricing_parse.run(company="ShelfMitra", week=1))["plans"]
    assert {p["plan"]: p["price_inr_per_month"] for p in plans} == {
        "Starter": 1299,
        "Growth": 2499,
        "Enterprise": None,
    }


def test_changelog_diff_returns_only_this_weeks_entries(world):
    added = json.loads(changelog_diff.run(company="StockSathi", week=2))["added"]
    assert [e["title"] for e in added] == ["New Pro price: ₹699/month", "Launch: UPI Udhaar"]
    week1 = json.loads(changelog_diff.run(company="BahiBook", week=1))["added"]
    assert [e["title"] for e in week1] == ["Fix: GST total rounding"]  # history entries excluded


def test_news_search_filters_by_credibility(world):
    tool = NewsSearchTool()
    everything = json.loads(tool.run(company="StockSathi", week=3))["articles"]
    credible = json.loads(tool.run(company="StockSathi", week=3, min_credibility=0.5))["articles"]
    assert {a["id"] for a in everything} == {"news-w3-2", "news-w3-3"}
    assert [a["id"] for a in credible] == ["news-w3-3"]  # the 0.2 rumor is filtered out


def test_tool_errors_are_strings_the_agent_can_read(world):
    assert NewsSearchTool().run(company="KhataKing", week=1).startswith("Error: unknown company")
    assert "HTTP 404" in pricing_parse.run(company="StockSathi", week=4)


def test_tools_have_the_names_the_prompt_uses():
    assert (pricing_parse.name, changelog_diff.name, NewsSearchTool().name) == (
        "pricing_parse",
        "changelog_diff",
        "news_search",
    )


def test_cache_function_caches_hits_only():
    assert cache_only_hits({}, json.dumps({"articles": [{"id": "x"}]})) is True
    assert cache_only_hits({}, json.dumps({"articles": []})) is False
    assert cache_only_hits({}, "Error: unknown company") is False
    assert NewsSearchTool().cache_function is cache_only_hits
