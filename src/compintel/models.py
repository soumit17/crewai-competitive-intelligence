"""Domain models (spec §5). M2: Scout reports. M3: threats, recommendations, the Brief. M4: Synthesis."""

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


class ThreatAssessment(BaseModel):
    company: CompanyName
    threat: Literal["low", "medium", "high"]
    rationale: str = Field(description="One or two sentences, citing signal ids in [brackets].")
    signal_ids: list[str] = Field(description="Ids of the signals this assessment rests on.")


class ThreatAssessments(BaseModel):
    """Container for a task's list output: output_pydantic takes a model class, not list[Model]."""

    threats: list[ThreatAssessment]


class Recommendation(BaseModel):
    action: str
    owner: Literal["Product", "Sales", "Marketing", "Leadership"]
    urgency: Literal["this_week", "this_month", "watch"]
    signal_ids: list[str]


class Brief(BaseModel):
    week: int
    headline: str
    tl_dr: list[str] = Field(max_length=5, description="Each item cites signal ids, e.g. [StockSathi-2-1].")
    since_last_week: list[str] = Field(description="What changed versus earlier weeks.")
    threats: list[ThreatAssessment]
    recommendations: list[Recommendation]
    watchlist: list[str]
    discounted_claims: list[str] = Field(
        default_factory=list, description="Low-credibility claims, each citing its signal id."
    )
    route: Literal["routine", "material"]


class Synthesis(BaseModel):
    """The deep dive's strategist output: the threat picture across the three analyses, with actions."""

    threats: list[ThreatAssessment]
    key_findings: list[str] = Field(description="At most 5, each citing signal ids in [brackets].")
    recommendations: list[Recommendation]
