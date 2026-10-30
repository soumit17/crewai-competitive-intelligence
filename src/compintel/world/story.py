"""The 3-week storyline (spec §4.3) as plain data. Everything generated in M1 is rendered from this file.

Week 1 = baseline, week 2 = material (price cut, UPI credit launch, Series B, Kolkata hiring),
week 3 = subtle (BahiBook GST e-invoicing PM, ShelfMitra Kolkata office, StockSathi rating dip, an
acquisition rumor from a low-credibility blog). All companies and news outlets are fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

WEEKS = (1, 2, 3)
WEEK_1_START = date(2026, 10, 5)


def week_start(week: int) -> date:
    return WEEK_1_START + timedelta(days=7 * (week - 1))


def week_day(week: int, offset: int) -> str:
    return (week_start(week) + timedelta(days=offset)).isoformat()


@dataclass(frozen=True)
class Company:
    name: str
    slug: str
    hq: str
    positioning: str


COMPANIES = (
    Company(
        "StockSathi",
        "stocksathi",
        "Bengaluru",
        "Price-aggressive, mobile-first inventory app for kirana stores",
    ),
    Company(
        "ShelfMitra", "shelfmitra", "Mumbai", "Well-funded, enterprise-leaning retail suite, expanding east"
    ),
    Company("BahiBook", "bahibook", "Pune", "Niche accounting-first GST bookkeeping for small shops"),
)
BY_NAME = {c.name: c for c in COMPANIES}


# ---------------------------------------------------------------- pricing pages
@dataclass(frozen=True)
class Plan:
    name: str
    price_inr: int | None  # None = "Contact sales"
    features: tuple[str, ...]
    was_inr: int | None = None  # struck-through old price, shown the week a price changes


def pricing(company: str, week: int) -> list[Plan]:
    if company == "StockSathi":
        pro = (
            Plan("Pro", 999, ("Unlimited items", "GST billing", "2 devices"))
            if week == 1
            else Plan("Pro", 699, ("Unlimited items", "GST billing", "2 devices", "UPI Udhaar"),
                      was_inr=999 if week == 2 else None)
        )  # fmt: skip
        return [
            Plan("Basic", 499, ("500 items", "GST billing", "1 device")),
            pro,
            Plan("Business", 1999, ("Unlimited items", "5 devices", "Multi-store", "Priority support")),
        ]
    if company == "ShelfMitra":
        return [
            Plan("Starter", 1299, ("Inventory + billing", "3 users")),
            Plan("Growth", 2499, ("Multi-store dashboard", "Purchase orders", "10 users")),
            Plan("Enterprise", None, ("Dedicated success manager", "ERP integrations", "Unlimited users")),
        ]
    return [
        Plan("Solo", 399, ("GST returns", "Ledger", "1 user")),
        Plan("Plus", 899, ("GST returns", "Ledger", "Inventory lite", "3 users")),
    ]


# ---------------------------------------------------------------- changelogs (cumulative; week 0 = history)
@dataclass(frozen=True)
class ChangelogEntry:
    week: int
    date: str
    title: str
    body: str


CHANGELOG: dict[str, tuple[ChangelogEntry, ...]] = {
    "StockSathi": (
        ChangelogEntry(
            0,
            "2026-09-08",
            "Barcode scanning on low-end Android",
            "Scan EAN barcodes on phones with 2 GB RAM.",
        ),
        ChangelogEntry(0, "2026-09-22", "Hindi and Kannada interface", "Switch language from Settings."),
        ChangelogEntry(1, week_day(1, 1), "Dark mode", "Easier on the eyes during late closing hours."),
        ChangelogEntry(1, week_day(1, 3), "Faster sync on 2G networks", "Sync payloads are now 40% smaller."),
        ChangelogEntry(
            2,
            week_day(2, 1),
            "Launch: UPI Udhaar",
            "Give store credit (udhaar) to regular customers and collect it later via UPI, with automatic "
            "reminders. Included in Pro.",
        ),
        ChangelogEntry(
            2, week_day(2, 2), "New Pro price: ₹699/month", "Pro drops from ₹999 to ₹699 per month."
        ),
        ChangelogEntry(
            3, week_day(3, 2), "Fix: UPI Udhaar reminders", "Fixed duplicate reminder notifications."
        ),
    ),
    "ShelfMitra": (
        ChangelogEntry(
            0, "2026-09-03", "Multi-store dashboard", "One view of stock across all your outlets."
        ),
        ChangelogEntry(0, "2026-09-25", "Tally export", "Export day books to Tally in one click."),
        ChangelogEntry(1, week_day(1, 2), "Purchase order approvals", "Owners approve POs from their phone."),
        ChangelogEntry(
            2, week_day(2, 3), "Bulk GST invoice export", "Download a month of invoices as one ZIP."
        ),
        ChangelogEntry(
            3, week_day(3, 1), "Bengali language support (beta)", "The app is now available in Bengali."
        ),
    ),
    "BahiBook": (
        ChangelogEntry(
            0, "2026-09-15", "GSTR-1 JSON export", "Upload-ready GSTR-1 files for the GST portal."
        ),
        ChangelogEntry(
            1, week_day(1, 2), "Fix: GST total rounding", "Totals now round per invoice, not per line."
        ),
        ChangelogEntry(
            2, week_day(2, 2), "Fix: A5 invoice layout", "PDF invoices print correctly on A5 paper."
        ),
        ChangelogEntry(
            3, week_day(3, 3), "Fix: ledger search", "Search now finds names written in Devanagari."
        ),
    ),
}


# ---------------------------------------------------------------- careers pages
@dataclass(frozen=True)
class Job:
    title: str
    location: str
    openings: int
    team: str


_KOLKATA_SALES = (
    Job("Field Sales Executive", "Kolkata", 12, "Sales"),
    Job("Territory Sales Manager", "Kolkata", 2, "Sales"),
    Job("Regional Sales Head, East", "Kolkata", 1, "Sales"),
)


def careers(company: str, week: int) -> list[Job]:
    if company == "StockSathi":
        return [
            Job("Android Engineer", "Bengaluru", 2, "Engineering"),
            Job("Customer Success", "Bengaluru", 1, "Support"),
        ]
    if company == "ShelfMitra":
        base = [
            Job("Backend Engineer", "Mumbai", 2, "Engineering"),
            Job("Product Designer", "Mumbai", 1, "Design"),
        ]
        return base + (list(_KOLKATA_SALES) if week >= 2 else [])
    base = [Job("Customer Support Associate", "Pune", 1, "Support")]
    return base + ([Job("Product Manager, GST e-invoicing", "Pune", 1, "Product")] if week >= 3 else [])


# ---------------------------------------------------------------- news (news.sqlite)
@dataclass(frozen=True)
class Article:
    id: str
    company: str
    week: int
    date: str
    title: str
    source: str
    credibility: float  # 0..1; < 0.5 must never be stated as fact (spec §8.2 guardrail)
    body: str


NEWS: tuple[Article, ...] = (
    Article("news-w1-1", "StockSathi", 1, week_day(1, 2), "StockSathi shares festive-season stocking tips",
            "StockSathi Blog", 0.6, "A checklist for kirana owners stocking up before Diwali."),
    Article("news-w1-2", "ShelfMitra", 1, week_day(1, 3), "ShelfMitra adds purchase-order approvals",
            "Retail Tech Daily", 0.85, "Multi-store chains can now route purchase orders to owners for approval."),
    Article("news-w1-3", "BahiBook", 1, week_day(1, 4), "BahiBook crosses 50,000 users in Maharashtra",
            "Pune Business Times", 0.7, "The Pune bookkeeping app says most users are single-counter shops."),
    Article("news-w2-1", "StockSathi", 2, week_day(2, 2), "StockSathi slashes Pro plan price by 30% to ₹699",
            "Retail Tech Daily", 0.9,
            "StockSathi cut its Pro plan from ₹999 to ₹699 a month, a 30% drop, pricing it below most rivals."),
    Article("news-w2-2", "StockSathi", 2, week_day(2, 1), "StockSathi launches UPI Udhaar credit for kiranas",
            "Retail Tech Daily", 0.85,
            "The feature lets shops extend credit to regulars and collect dues over UPI."),
    Article("news-w2-3", "ShelfMitra", 2, week_day(2, 3), "ShelfMitra raises ₹200 crore Series B",
            "The Mumbai Ledger", 0.9,
            "ShelfMitra raised a ₹200 crore Series B led by Deccan Growth Partners to expand into eastern India."),
    Article("news-w2-4", "BahiBook", 2, week_day(2, 4), "BahiBook to host free GST filing webinar",
            "BahiBook Blog", 0.6, "A one-hour session on GSTR-1 for first-time filers."),
    Article("news-w3-1", "ShelfMitra", 3, week_day(3, 1), "ShelfMitra opens eastern hub in Kolkata",
            "Retail Tech Daily", 0.85,
            "ShelfMitra has opened an office in Kolkata's Salt Lake Sector V to serve West Bengal retailers."),
    Article("news-w3-2", "StockSathi", 3, week_day(3, 3), "Exclusive?! StockSathi about to be acquired, say sources",
            "Startup Gupshup (blog)", 0.2,
            "Unnamed sources claim a large e-commerce player is acquiring StockSathi. No company confirmed it."),
    Article("news-w3-3", "StockSathi", 3, week_day(3, 4), "StockSathi hosts kirana owners' meetup in Mysuru",
            "StockSathi Blog", 0.6, "Around 80 store owners attended a product feedback session."),
)  # fmt: skip


# ---------------------------------------------------------------- market data (market.sqlite, served by MCP)
@dataclass(frozen=True)
class FundingRound:
    company: str
    round: str
    amount_inr_cr: float
    date: str
    week: int  # week it became public; 0 = before the simulation
    lead_investor: str


FUNDING: tuple[FundingRound, ...] = (
    FundingRound("StockSathi", "Seed", 8, "2023-06-14", 0, "Konark Ventures"),
    FundingRound("StockSathi", "Series A", 60, "2025-02-10", 0, "Malabar Capital"),
    FundingRound("ShelfMitra", "Seed", 10, "2022-03-01", 0, "Konark Ventures"),
    FundingRound("ShelfMitra", "Series A", 75, "2024-08-20", 0, "Sahyadri Partners"),
    FundingRound("ShelfMitra", "Series B", 200, week_day(2, 3), 2, "Deccan Growth Partners"),
    FundingRound("BahiBook", "Seed", 3, "2021-11-05", 0, "Pune Angels Network"),
)


@dataclass(frozen=True)
class RatingWeek:
    company: str
    week: int  # -1 and 0 = history before the simulation, for trends
    rating: float
    top_complaint: str
    review_count: int = field(default=0)  # filled by the seeded generator


RATINGS: tuple[RatingWeek, ...] = (
    *(RatingWeek("StockSathi", w, 4.4, "Occasional sync delays") for w in (-1, 0, 1)),
    RatingWeek("StockSathi", 2, 4.3, "Occasional sync delays"),
    RatingWeek("StockSathi", 3, 3.9, "UPI Udhaar reminders sent twice; customers confused"),
    *(RatingWeek("ShelfMitra", w, 4.3, "Setup takes too long") for w in (-1, 0, 1, 2, 3)),
    *(RatingWeek("BahiBook", w, 4.1, "Inventory features are basic") for w in (-1, 0, 1, 2, 3)),
)


# ---------------------------------------------------------------- ground truth (spec §4.4)
# material_signals: must be found and drive the "material" route. must_surface: subtle signals a routine brief
# must still mention. must_not_claim: phrases that must never appear as fact. must_discount: claims that belong
# in Brief.discounted_claims. detail_contains: substrings (case-insensitive) a correct Signal.detail includes.
NOT_CLAIMED = ["StockSathi acquired", "StockSathi has been acquired", "acquisition of StockSathi"]

GROUND_TRUTH: dict[int, dict] = {
    1: {
        "week": 1,
        "expected_route": "routine",
        "material_signals": [],
        "must_surface": [],
        "must_not_claim": NOT_CLAIMED,
        "must_discount": [],
        "expected_threat": {"StockSathi": "low", "ShelfMitra": "low", "BahiBook": "low"},
        "notes": "Baseline: blog posts, small features and bug fixes only.",
    },
    2: {
        "week": 2,
        "expected_route": "material",
        "material_signals": [
            {"company": "StockSathi", "kind": "pricing_change", "detail_contains": ["699", "30%"]},
            {"company": "StockSathi", "kind": "feature_launch", "detail_contains": ["UPI", "credit"]},
            {"company": "ShelfMitra", "kind": "funding", "detail_contains": ["Series B", "200"]},
            {"company": "ShelfMitra", "kind": "hiring_expansion", "detail_contains": ["Kolkata"]},
        ],
        "must_surface": [],
        "must_not_claim": NOT_CLAIMED,
        "must_discount": [],
        "expected_threat": {"StockSathi": "high", "ShelfMitra": "high", "BahiBook": "low"},
        "notes": "StockSathi undercuts our ₹799 plan; ShelfMitra is funded and hiring 15 sales roles in our home city.",
    },
    3: {
        "week": 3,
        "expected_route": "routine",
        "material_signals": [],
        "must_surface": [
            {"company": "BahiBook", "kind": "hiring_expansion", "detail_contains": ["GST e-invoicing"]},
            {"company": "ShelfMitra", "kind": "hiring_expansion", "detail_contains": ["Kolkata", "office"]},
            {"company": "StockSathi", "kind": "rating_change", "detail_contains": ["3.9"]},
        ],
        "must_not_claim": NOT_CLAIMED,
        "must_discount": [{"company": "StockSathi", "detail_contains": ["acqui"]}],
        "expected_threat": {"StockSathi": "medium", "ShelfMitra": "high", "BahiBook": "medium"},
        "notes": (
            "Routine: the Kolkata office continues week 2's expansion (an update, not new). BahiBook's GST e-invoicing PM "
            "hints at overlap with our 2027 roadmap. The acquisition rumor (credibility 0.2) must be discounted."
        ),
    },
}
