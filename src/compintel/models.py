"""Domain models (spec §5). M2: what a Scout returns. Brief, threats and flow state arrive with M3–M5."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CompanyName = Literal["StockSathi", "ShelfMitra", "BahiBook"]


class Signal(BaseModel):
    id: str = Field(description='"<Company>-<week>-<n>", e.g. "StockSathi-2-1"')
    company: CompanyName
    kind: Literal[
        "pricing_change",
        "feature_launch",
        "funding",
        "hiring_expansion",
        "partnership",
        "rating_change",
        "leadership",
        "rumor",
        "other",
    ]
    detail: str = Field(
        description="One factual sentence with the concrete numbers (prices, amounts, places)."
    )
    source_ref: str = Field(description="URL of the page or news article id the signal came from.")
    source_credibility: float = Field(
        ge=0, le=1, description="Pages and market data 0.9; news: the article's score."
    )
    novelty: Literal["new", "update", "repeat"] = Field(description="Compared with the previous week.")
    impact_on_us: Literal["none", "low", "medium", "high"] = Field(description="Impact on KiranaCloud.")


class SignalReport(BaseModel):
    company: CompanyName
    week: int
    signals: list[Signal]
    summary: str = Field(description="Two sentences at most.")
