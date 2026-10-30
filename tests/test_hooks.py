"""M2 url_allowlist hook: unit cases, then inside a real (scripted) agent run."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from crewai.hooks import HookAborted

from compintel import hooks, scouting
from tests.fakes import Rule, ScriptedLLM, ToolCall, tool_results


def ctx(url):
    return SimpleNamespace(tool_input={"website_url": url} if url is not None else {})


def test_fake_web_urls_pass(world):
    hooks.url_allowlist(ctx(f"{world}/week-2/shelfmitra/careers.html"))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://127.0.0.1:9999/week-1/stocksathi/pricing.html",  # right host, wrong port
        "http://localhost:8765/",  # different hostname for the same machine
        "http://127.0.0.1:8765@evil.example/",  # userinfo trick: real host is evil.example
        "file:///etc/passwd",
        "",
        None,
    ],
)
def test_everything_else_is_aborted(world, url):
    with pytest.raises(HookAborted, match="URL not allowed"):
        hooks.url_allowlist(ctx(url))


REPORT = {"company": "StockSathi", "week": 2, "summary": "-", "signals": []}


def test_blocked_tool_call_reaches_the_agent_as_a_tool_result_and_the_run_continues(world):
    """HookAborted at PRE_TOOL_CALL blocks that one call (the agent sees it, can recover); at PRE_MODEL_CALL
    (budget gate, M0) it ends the whole run."""
    hooks.install()
    seen = {}

    def step(messages, response_model):
        if response_model is not None:
            return REPORT
        results = tool_results(messages)
        if '"role": "tool"' not in results:
            return [ToolCall("read_website_content", {"website_url": "https://example.com/"})]
        seen["result"] = results
        return "Final Answer: nothing found"

    report = asyncio.run(scouting.scout_company("StockSathi", 2, llm=ScriptedLLM(Rule(step))))
    assert report.company == "StockSathi"
    assert "blocked by hook" in json.loads(seen["result"])[0]["content"]


def test_scouts_refuse_to_start_without_the_allowlist():
    # conftest cleared all hooks; the scrape tool's own SSRF guard is off, so no allowlist = no scout.
    with pytest.raises(RuntimeError, match="url_allowlist"):
        scouting.make_scout(2, llm=ScriptedLLM())
