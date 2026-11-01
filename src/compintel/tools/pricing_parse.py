"""PricingParseTool (spec §6) twice: the `@tool` decorator version (M2's demo of the decorator) and the
BaseTool twin every agent uses since M6, because crew checkpoints can't serialize a `@tool`'s function.
Parsing prices in code beats asking the LLM to read HTML:
exact numbers, no tokens spent on markup, and the week-over-week diff is computed, not guessed."""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup
from crewai.tools import BaseTool, tool
from pydantic import BaseModel, Field

from compintel.tools.fakeweb_client import FakeWebError, fetch


def parse_plans(html: str) -> list[dict]:
    plans = []
    for div in BeautifulSoup(html, "html.parser").select("div.plan"):
        price_el = div.select_one(".price")
        was_el = price_el.select_one("s.was") if price_el else None
        was = int(re.sub(r"\D", "", was_el.get_text())) if was_el else None
        if was_el:
            was_el.extract()
        digits = re.sub(r"\D", "", price_el.get_text()) if price_el else ""
        plans.append(
            {
                "plan": div["data-plan"],
                "price_inr_per_month": int(digits) if digits else None,  # None = "Contact sales"
                "previous_price_inr": was,
                "features": [li.get_text(strip=True) for li in div.select("li")],
            }
        )
    return plans


def price_changes(previous: list[dict], current: list[dict]) -> list[str]:
    before = {p["plan"]: p["price_inr_per_month"] for p in previous}
    changes = []
    for p in current:
        old, new = before.get(p["plan"]), p["price_inr_per_month"]
        if p["plan"] not in before:
            changes.append(f"New plan {p['plan']} at ₹{new}/month")
        elif old and new and old != new:
            changes.append(f"{p['plan']}: ₹{old} → ₹{new}/month ({(new - old) / old:+.0%})")
    changes += [f"Plan {name} removed" for name in before if name not in {p["plan"] for p in current}]
    return changes


DESCRIPTION = (
    "Get a competitor's pricing plans for a week as JSON, plus the price changes since the previous week. "
    "Prices are INR per month. Use this instead of reading the pricing page yourself."
)


def pricing_report(company: str, week: int) -> str:
    try:
        current = parse_plans(fetch(company, week, "pricing"))
        previous = parse_plans(fetch(company, week - 1, "pricing")) if week > 1 else []
    except FakeWebError as e:
        return f"Error: {e}"  # a string, not an exception, so the agent can recover (spec §6)
    return json.dumps(
        {
            "company": company,
            "week": week,
            "plans": current,
            "changes_since_last_week": price_changes(previous, current) if week > 1 else "n/a (first week)",
        },
        ensure_ascii=False,
    )


@tool("pricing_parse")
def pricing_parse(company: str, week: int) -> str:
    """Get a competitor's pricing plans for a week as JSON, plus the price changes since the previous week.
    Prices are INR per month. Use this instead of reading the pricing page yourself."""
    return pricing_report(company, week)


class CompanyWeek(BaseModel):
    company: str = Field(description='Exact company name, e.g. "StockSathi".')
    week: int = Field(ge=1, description="Week number.")


class PricingParseTool(BaseTool):
    """The same tool as a BaseTool, for crews that checkpoint: a `@tool` keeps the raw function in a field
    crewai 1.15.33 can't serialize, so every checkpoint write of an agent holding one fails (seen in M5)."""

    name: str = "pricing_parse"
    description: str = DESCRIPTION
    args_schema: type[BaseModel] = CompanyWeek

    def _run(self, company: str, week: int) -> str:
        return pricing_report(company, week)
