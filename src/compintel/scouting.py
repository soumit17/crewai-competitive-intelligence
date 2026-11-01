"""Scouting (spec §8.1): one Signal Scout per competitor, run concurrently, each returning a SignalReport.

Two implementations of the same fan-out, kept side by side for the M2 comparison (docs/decisions.md):

- `scout_all`      — three single agents, `Agent.kickoff_async(..., response_format=SignalReport)` under
                     `asyncio.gather`. No Crew, no Task: the agent *is* the unit of work. The Flow uses this.
- `scout_all_crew` — a one-agent, one-task `Crew` run per company with `crew.kickoff_for_each(inputs=[...])`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from crewai import Agent, BaseLLM, Crew, CrewOutput, Process, Task
from crewai.mcp import MCPServerStdio
from crewai.utilities.pydantic_schema_utils import generate_model_description
from crewai_tools import ScrapeWebsiteTool

from compintel import hooks
from compintel.config import AGENT_LIMITS, LONG_OUTPUT_MAX_TOKENS, get_settings, make_llm
from compintel.models import SignalReport
from compintel.tools.changelog_diff import ChangelogDiffTool
from compintel.tools.news_search import NewsSearchTool
from compintel.tools.pricing_parse import PricingParseTool
from compintel.world.story import BY_NAME, COMPANIES

SCOUT_ROLE = "Competitive Signal Scout"
MARKET_COMMAND = "market-signals"
# A scout makes ~6 tool calls (pricing, changelog, careers ×2, news, funding, ratings) before answering.
SCOUT_LIMITS = {**AGENT_LIMITS, "max_iter": 10}
# During the tool loop crewai sends no response schema, so the model can't know SignalReport's fields; only
# the Converter (a separate LLM call after the loop) sees it. Put the same schema in the prompt, built by the
# same crewai helper: the final answer then validates directly and the conversion call is skipped.
REPORT_SCHEMA = json.dumps(generate_model_description(SignalReport)["json_schema"]["schema"])


def market_mcp(week: int) -> MCPServerStdio:
    """The market-signals MCP server, launched over stdio by crewai when the agent starts (the `mcps` DSL).

    MARKET_AS_OF_WEEK makes the server's "today" this week, so a week-1 scout can't see week-2 funding.
    crewai merges `env` with a safe default env (PATH, HOME...); our API keys are not passed to it.
    """
    # crewai names MCP tools "<command>_<args>_<tool>" (64-char cap). A bare console-script command gives
    # market_signals_funding_rounds; a full interpreter path would be truncated into an unreadable hash.
    if shutil.which(MARKET_COMMAND) is None:
        raise RuntimeError(
            f"`{MARKET_COMMAND}` not on PATH; run through `uv run` (it's a project console script)"
        )
    return MCPServerStdio(
        command=MARKET_COMMAND,
        args=[],
        env={"MARKET_AS_OF_WEEK": str(week), "MARKET_DB": str(get_settings().p3_data_dir / "market.sqlite")},
    )


def make_scout(week: int, llm: BaseLLM | None = None, verbose: bool = False) -> Agent:
    # Fail closed: the scrape tool's SSRF guard is off in compintel runs, so the allowlist hook must be on.
    if not hooks.is_installed(hooks.url_allowlist):
        raise RuntimeError("url_allowlist hook not installed; call compintel.hooks.install() first")
    return Agent(
        role=SCOUT_ROLE,
        goal="Find every new, verifiable signal about one competitor in one week, with numbers and sources.",
        backstory=(
            "You track Indian retail-tech rivals for KiranaCloud, a Kolkata kirana-store SaaS "
            "(₹799/month Dukaan plan). You report facts, cite sources, and never present rumors as facts."
        ),
        llm=llm or make_llm(max_tokens=LONG_OUTPUT_MAX_TOKENS),
        # BaseTool twins, not the @tool versions: inside the Flow every crew checkpoint serializes all of the
        # run's agents, scouts included, and a @tool's raw function can't be (M6: the ShortUpdateCrew's
        # checkpoints all failed live, with only a logged warning).
        tools=[NewsSearchTool(), ScrapeWebsiteTool(), PricingParseTool(), ChangelogDiffTool()],
        mcps=[market_mcp(week)],
        verbose=verbose,
        **SCOUT_LIMITS,
    )


def scout_prompt(company: str, week: int) -> str:
    base = get_settings().fakeweb_url
    slug = BY_NAME[company].slug
    prev = (
        f"\n  Last week's careers page, to tell new roles from old: "
        f"{base}/week-{week - 1}/{slug}/careers.html"
        if week > 1
        else ""
    )
    return f"""Scout {company} for week {week}. Check every source:
- Pricing: pricing_parse(company="{company}", week={week}) (includes changes since last week).
- Product: changelog_diff(company="{company}", week={week}) (only entries added this week).
- Hiring: read {base}/week-{week}/{slug}/careers.html with the website tool.{prev}
- News: news_search(company="{company}", week={week}).
- Market: market_signals_funding_rounds(company="{company}") and
  market_signals_app_rating_trend(company="{company}", weeks=3).
  Report a funding round only if announced_week is {week}; report a rating only if it changed this week.

Rules:
- One Signal per distinct fact. ids are "{company}-{week}-1", "{company}-{week}-2", ...
- detail: one factual sentence with the concrete numbers and places from the source.
- source_ref: the page URL or the news article id. source_credibility: 0.9 for pages and market data,
  the article's credibility for news.
- An article with credibility below 0.5 is kind "rumor", whatever it claims.
- novelty: "new" if it appeared this week, "update" if it continues something from earlier weeks.
- impact_on_us: judge for KiranaCloud (Kolkata stores, ₹799/month, plans GST e-invoicing and UPI credit).
- Include routine items (bug fixes, blog posts) with impact "none" or "low"; leave out nothing you found.
- Final answer: only the SignalReport JSON object matching this schema, no prose and no code fences:
{REPORT_SCHEMA}"""


async def scout_company(
    company: str, week: int, llm: BaseLLM | None = None, verbose: bool = False
) -> SignalReport:
    agent = make_scout(week, llm, verbose)
    # kickoff_async: the non-blocking twin of Agent.kickoff, so three scouts can wait on I/O concurrently.
    out = await agent.kickoff_async(scout_prompt(company, week), response_format=SignalReport)
    if not isinstance(out.pydantic, SignalReport):
        raise ValueError(f"{company} scout returned no SignalReport: {out.raw[:300]!r}")
    return out.pydantic


async def scout_all(week: int, llm: BaseLLM | None = None, verbose: bool = False) -> dict[str, SignalReport]:
    """Fan out one scout per competitor concurrently; each builds its own LLM unless one is injected."""
    reports = await asyncio.gather(*(scout_company(c.name, week, llm, verbose) for c in COMPANIES))
    return {r.company: r for r in reports}


def scout_all_crew(week: int, llm: BaseLLM | None = None, verbose: bool = False) -> dict[str, SignalReport]:
    """Alternative: the same scout as a one-task Crew, run once per company by `kickoff_for_each`.

    `{company}`/`{week}`/`{instructions}` in the task are interpolated from each input dict. The Crew adds a
    Task layer (expected_output, output_pydantic) and per-run crew bookkeeping on top of the same agent.
    """
    task = Task(
        description="{instructions}",
        expected_output="A SignalReport for {company}, week {week}.",
        agent=make_scout(week, llm, verbose),
        output_pydantic=SignalReport,
    )
    crew = Crew(agents=[task.agent], tasks=[task], process=Process.sequential, verbose=verbose)  # type: ignore[list-item]
    inputs: list[dict[str, Any]] = [
        {"company": c.name, "week": week, "instructions": scout_prompt(c.name, week)} for c in COMPANIES
    ]
    results = crew.kickoff_for_each(inputs=inputs)  # sequential: one crew run per input dict
    # CrewStreamingOutput only comes back with stream=True, which this crew doesn't set.
    reports = [r.pydantic for r in results if isinstance(r, CrewOutput)]
    return {r.company: r for r in reports if isinstance(r, SignalReport)}
