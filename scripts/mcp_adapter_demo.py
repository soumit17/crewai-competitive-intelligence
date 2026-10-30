"""MCP two ways (spec §6): crewai's `mcps` DSL (what the scouts use) vs crewai-tools' MCPServerAdapter. $0.

    uv sync --group mcp-adapter
    uv run python scripts/mcp_adapter_demo.py

DSL:     Agent(mcps=[MCPServerStdio(...)]): crewai starts the server, discovers tools when the agent runs, and
         prefixes names with the server command (market_signals_funding_rounds). Lifecycle is crewai's.
Adapter: `with MCPServerAdapter(params) as tools:` you start the server and get ordinary BaseTool objects
         (funding_rounds), usable anywhere a tool is (any agent, a Task's tools, called directly), but only
         inside the `with` block, which you own.

The difference that bit us in M2: `Agent.copy()` (used by `Crew.kickoff_for_each`) drops `mcps` but keeps
`tools`, so DSL servers vanish from copied agents while adapter tools survive.
"""

import json

from crewai import Agent
from crewai.mcp import MCPServerStdio
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

from compintel.config import apply_env

ENV = {"MARKET_AS_OF_WEEK": "2"}


def agent(**kwargs) -> Agent:
    return Agent(role="Demo", goal="Demo", backstory="Demo", llm="anthropic/claude-haiku-5-5", **kwargs)


if __name__ == "__main__":
    apply_env()
    with MCPServerAdapter(StdioServerParameters(command="market-signals", args=[], env=ENV)) as tools:
        print("adapter tool names:", [t.name for t in tools])
        funding = next(t for t in tools if t.name == "funding_rounds")
        rounds = json.loads(funding.run(company="ShelfMitra"))["rounds"]
        print("called directly, no LLM:", [r["round"] for r in rounds])
        copied = agent(tools=list(tools)).copy()
        print("adapter tools after Agent.copy():", [t.name for t in copied.tools or []])

    dsl = agent(mcps=[MCPServerStdio(command="market-signals", args=[], env=ENV)])
    print("DSL mcps before copy:", len(dsl.mcps or []), "| after Agent.copy():", dsl.copy().mcps)
