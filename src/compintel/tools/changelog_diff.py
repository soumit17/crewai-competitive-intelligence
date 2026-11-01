"""ChangelogDiffTool (spec §6): `@tool` decorator (scouts) and a BaseTool twin (checkpointed deep dive).
Diffs this week's changelog page against last week's so the
agent gets only what's new, not a page of history it would have to compare itself."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup
from crewai.tools import BaseTool, tool
from pydantic import BaseModel

from compintel.tools.fakeweb_client import FakeWebError, fetch
from compintel.tools.pricing_parse import CompanyWeek
from compintel.world.story import week_start


def parse_entries(html: str) -> list[dict]:
    entries = []
    for a in BeautifulSoup(html, "html.parser").select("article.entry"):
        title, body = a.select_one("h3"), a.select_one("p")
        entries.append(
            {
                "date": a["data-date"],
                "title": title.get_text(strip=True) if title else "",
                "detail": body.get_text(strip=True) if body else "",
            }
        )
    return entries


def added_entries(company: str, week: int) -> list[dict]:
    current = parse_entries(fetch(company, week, "changelog"))
    if week > 1:
        seen = {(e["date"], e["title"]) for e in parse_entries(fetch(company, week - 1, "changelog"))}
        return [e for e in current if (e["date"], e["title"]) not in seen]
    # No week-0 page: for week 1, "new" means dated within week 1.
    return [e for e in current if e["date"] >= week_start(1).isoformat()]


DESCRIPTION = (
    "List the product changelog entries a competitor added in a given week (new since the previous week), "
    "newest first, as JSON. Use this instead of reading the changelog page yourself."
)


def changelog_report(company: str, week: int) -> str:
    try:
        added = added_entries(company, week)
    except FakeWebError as e:
        return f"Error: {e}"
    return json.dumps({"company": company, "week": week, "added": added}, ensure_ascii=False)


@tool("changelog_diff")
def changelog_diff(company: str, week: int) -> str:
    """List the product changelog entries a competitor added in a given week (new since the previous week),
    newest first, as JSON. Use this instead of reading the changelog page yourself."""
    return changelog_report(company, week)


class ChangelogDiffTool(BaseTool):
    """BaseTool twin for checkpointed crews (see pricing_parse.PricingParseTool)."""

    name: str = "changelog_diff"
    description: str = DESCRIPTION
    args_schema: type[BaseModel] = CompanyWeek

    def _run(self, company: str, week: int) -> str:
        return changelog_report(company, week)
