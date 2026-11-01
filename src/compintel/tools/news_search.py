"""NewsSearchTool (spec §6): a `BaseTool` subclass with a typed `args_schema` and a `cache_function`.

`args_schema` is what the LLM sees as the tool's parameters (names, types, descriptions) and what crewai
validates the call against. `cache_function(args, result) -> bool` decides whether crewai's tool cache may
store this result: here only non-empty results, so a search that found nothing is retried next time.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from compintel.config import get_settings
from compintel.world.story import BY_NAME


class NewsSearchArgs(BaseModel):
    company: str = Field(description='Exact company name, e.g. "ShelfMitra".')
    week: int = Field(ge=1, le=3, description="Week number to search.")
    min_credibility: float = Field(0.0, ge=0, le=1, description="Only articles at or above this credibility.")


def cache_only_hits(args: dict, result: str) -> bool:
    return result.startswith("{") and '"articles": []' not in result


class NewsSearchTool(BaseTool):
    name: str = "news_search"
    description: str = (
        "Search news articles about a competitor published in a given week. Each article has an id, title, "
        "source, credibility (0-1; below 0.5 means unverified) and a snippet. Cite articles by id."
    )
    args_schema: type[BaseModel] = NewsSearchArgs
    db_path: Path = Field(default_factory=lambda: get_settings().p3_data_dir / "news.sqlite")

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("cache_function", cache_only_hits)
        super().__init__(**kwargs)

    def _run(self, company: str, week: int, min_credibility: float = 0.0) -> str:
        if company not in BY_NAME:
            return f"Error: unknown company {company!r}. Known companies: {', '.join(BY_NAME)}."
        if not self.db_path.exists():
            return "Error: news database not found; run `uv run python scripts/gen_world.py`."
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT id, title, source, source_credibility, body FROM articles "
                "WHERE company = ? AND week = ? AND source_credibility >= ? ORDER BY date",
                (company, week, min_credibility),
            ).fetchall()
        if not rows:
            # Said plainly: a bare {"articles": []} led a live scout to repeat the same search until crewai's
            # loop detector stopped it (5 identical calls), failing the whole week's scouting.
            return (
                f"No news articles about {company} in week {week}. "
                "This is a complete answer; don't search again."
            )
        articles = [
            {"id": i, "title": t, "source": s, "credibility": c, "snippet": b} for i, t, s, c, b in rows
        ]
        return json.dumps({"company": company, "week": week, "articles": articles}, ensure_ascii=False)
