"""`market-signals` MCP server (spec §4.2): funding rounds and app-rating trends from data/market.sqlite.

Run over stdio by scripts/mcp_market_signals.py; M2 agents connect to it through crewai's `mcps` field.

"As of" week: the server only returns data public by `MARKET_AS_OF_WEEK` (set per run through the MCP config's
`env`). That keeps a week-1 run from seeing week-2 news by construction, instead of trusting the agent to ask
for the right week.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from compintel.world.story import BY_NAME


def build_server(db_path: Path, as_of_week: int) -> FastMCP:
    mcp = FastMCP("market-signals", log_level="WARNING")  # INFO logs every request to stderr

    def _query(sql: str, *params: Any) -> list[dict[str, Any]]:
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql, params)]

    def _unknown(company: str) -> str | None:
        if company in BY_NAME:
            return None
        return f"Unknown company {company!r}. Known companies: {', '.join(BY_NAME)}."

    # Docstrings and parameter names are what the LLM sees as the tool description.
    @mcp.tool()
    def funding_rounds(company: str) -> dict[str, Any] | str:
        """All publicly known funding rounds for a competitor, oldest first.

        Args:
            company: Exact company name, e.g. "ShelfMitra".
        """
        if err := _unknown(company):
            return err
        rows = _query(
            "SELECT round, amount_inr_cr, date, lead_investor, week AS announced_week FROM funding_rounds "
            "WHERE company = ? AND week <= ? ORDER BY date",
            company,
            as_of_week,
        )
        return {"company": company, "as_of_week": as_of_week, "currency": "INR crore", "rounds": rows}

    @mcp.tool()
    def app_rating_trend(company: str, weeks: int = 4) -> dict[str, Any] | str:
        """Weekly app-store rating for a competitor over the last `weeks` weeks + top complaints.

        Args:
            company: Exact company name, e.g. "StockSathi".
            weeks: How many recent weeks to return (1-8).
        """
        if err := _unknown(company):
            return err
        weeks = max(1, min(int(weeks), 8))
        rows = _query(
            "SELECT week, rating, review_count, top_complaint FROM app_ratings "
            "WHERE company = ? AND week <= ? "
            "ORDER BY week DESC LIMIT ?",
            company,
            as_of_week,
            weeks,
        )
        rows.reverse()
        change = round(rows[-1]["rating"] - rows[0]["rating"], 2) if len(rows) > 1 else 0.0
        return {"company": company, "as_of_week": as_of_week, "ratings": rows, "change_over_period": change}

    return mcp


def main() -> None:
    """Console script `market-signals` (stdio).

    crewai prefixes MCP tool names with the server's command line, so a short command keeps them readable:
    market_signals_funding_rounds, market_signals_app_rating_trend.
    """
    import os

    from compintel.config import get_settings

    db = Path(os.environ.get("MARKET_DB", get_settings().p3_data_dir / "market.sqlite"))
    build_server(db, as_of_week=int(os.environ.get("MARKET_AS_OF_WEEK", "3"))).run()  # stdio transport
