"""Domain models (spec §5). M2: Scout reports. M3: threats, recommendations, the Brief. M4: Synthesis.
M5: the Flow's state (CIState) and what it carries from week to week."""

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


class WeekSummary(BaseModel):
    """What the Flow carries forward about a published week: enough for routing and "since last week"."""

    week: int
    route: str
    headline: str
    threats: dict[str, str]  # company -> threat level
    published_path: str | None = None


class TieBreak(BaseModel):
    """The router's one LLM call, used only when the deterministic score lands exactly on the threshold."""

    route: Literal["routine", "material"]
    reason: str = Field(description="One sentence.")


class CIState(BaseModel):
    """CompetitiveIntelFlow's typed state (spec §5). @persist saves it after every method; next week's run
    forks it.

    `id` is added by crewai (the flow execution id). Per-week fields are reset when a new week starts;
    `history`
    is what survives the fork.
    """

    week: int = 1
    previous_state_id: str | None = None
    history: list[WeekSummary] = Field(default_factory=list)
    reports: dict[str, SignalReport] = Field(default_factory=dict)
    our_dukaan_price: int | None = None  # from the knowledge digest (a parallel @start)
    materiality_score: float = 0.0
    materiality_reasons: list[str] = Field(default_factory=list)
    route: str = ""
    deep_dive_variant: str = "manager_agent"
    brief: Brief | None = None
    brief_markdown: str = ""
    revision_count: int = 0  # M6 (human feedback)
    feedback_log: list[str] = Field(default_factory=list)  # M6
    published_path: str | None = None
