"""market-signals MCP server over stdio (spec §4.2).

Same server as the `market-signals` console script that agents launch through their `mcps` config:

    MARKET_AS_OF_WEEK=2 uv run python scripts/mcp_market_signals.py
"""

from compintel.world.market import main

if __name__ == "__main__":
    main()
