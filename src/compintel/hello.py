"""M0 hello: one Agent, no Crew, typed output. A preview of how the Scout works in M2.

`Agent.kickoff(...)` runs a single agent directly (crewai calls this a LiteAgent run): no tasks, no crew,
no process. With `response_format=<Pydantic model>` crewai asks the LLM for that schema and returns
the parsed object on `.pydantic`.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Literal

from crewai import Agent, BaseLLM
from crewai.lite_agent_output import LiteAgentOutput
from pydantic import BaseModel, Field

from compintel.config import AGENT_LIMITS, make_llm

DEFAULT_HEADLINE = "StockSathi cuts its Pro plan from ₹999 to ₹699 a month, starting Monday."


class HeadlineTake(BaseModel):
    """A one-headline version of the spec's Signal model (§5)."""

    company: str
    kind: Literal["pricing_change", "feature_launch", "funding", "hiring_expansion", "rumor", "other"]
    impact_on_us: Literal["none", "low", "medium", "high"]
    why: str = Field(description="One sentence, for KiranaCloud's leadership.")


def make_agent(llm: BaseLLM | None = None, verbose: bool = False) -> Agent:
    # Short role/goal/backstory: they are resent on every call, so length is paid per call (spec §7).
    return Agent(
        role="Competitive Signal Scout",
        goal="Classify one competitor headline for KiranaCloud, a Kolkata kirana-store SaaS at ₹799/month.",
        backstory="You read Indian retail-tech news and judge what matters to KiranaCloud. Be terse.",
        llm=llm or make_llm(),
        verbose=verbose,
        **AGENT_LIMITS,
    )


def run(
    headline: str = DEFAULT_HEADLINE, llm: BaseLLM | None = None, verbose: bool = False
) -> LiteAgentOutput:
    agent = make_agent(llm, verbose)
    out = agent.kickoff(f"Classify this headline: {headline}", response_format=HeadlineTake)
    # kickoff() detects a running event loop (i.e. inside a Flow) and returns a coroutine for the Flow to
    # await. Here we're plain sync code; inside a Flow, use `await agent.kickoff_async(...)` (M2 Scout).
    if isinstance(out, Coroutine):
        out.close()
        raise RuntimeError("hello.run() called inside an event loop; use Agent.kickoff_async there")
    return out
