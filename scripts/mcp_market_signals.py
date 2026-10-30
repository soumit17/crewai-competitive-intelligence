"""market-signals MCP server over stdio (spec §4.2).

Normally launched by an agent's `mcps` config, not by hand:

MARKET_AS_OF_WEEK=2 uv run python scripts/mcp_market_signals.py
"""

import os
from pathlib import Path

from compintel.config import PROJECT_ROOT
from compintel.world.market import build_server

if __name__ == "__main__":
    db = Path(os.environ.get("MARKET_DB", PROJECT_ROOT / "data" / "market.sqlite"))
    build_server(db, as_of_week=int(os.environ.get("MARKET_AS_OF_WEEK", "3"))).run()  # stdio transport
