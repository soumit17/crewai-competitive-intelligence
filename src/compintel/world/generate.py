"""Render the storyline (story.py) into the files the agents read (spec §4.5). Seeded and deterministic.

data/fakeweb/week-N/<slug>/{pricing,changelog,careers}.html   served by fakeweb.py on 127.0.0.1:8765
data/news.sqlite                                               read by NewsSearchTool (M2)
data/market.sqlite                                             served by the market-signals MCP server
data/ground_truth/week-N.json                                  read by tests and evals
knowledge/*.md                                                 KiranaCloud's own docs (knowledge sources, M4)
"""

from __future__ import annotations

import html
import json
import random
import shutil
import sqlite3
from pathlib import Path

from compintel.world import story

SEED = 20261005

# KiranaCloud's own documents. Note the deliberate overlaps the brief should notice:
# UPI credit (StockSathi, week 2) and GST e-invoicing (BahiBook, week 3) are both on our roadmap.
KNOWLEDGE = {
    "kiranacloud_pricing.md": """# KiranaCloud pricing (effective April 2026)

| Plan | Price | Includes |
|---|---|---|
| Lite | ₹399/month | 300 items, GST billing, 1 device |
| Dukaan (most popular) | ₹799/month | Unlimited items, GST billing, inventory alerts, 2 devices |
| Multi-store | ₹1,499/month | Everything in Dukaan, up to 3 outlets, 6 devices |

- Annual billing: 2 months free.
- 74% of paying stores are on Dukaan. Dukaan's price is our main competitive exposure.
- Floor: leadership has said we will not go below ₹649/month on Dukaan in 2026 without a board decision.
""",
    "kiranacloud_roadmap_2027.md": """# KiranaCloud product roadmap 2027 (internal)

| Quarter | Theme | Notes |
|---|---|---|
| Q1 2027 | GST e-invoicing | IRN generation for stores over the e-invoicing threshold. Biggest 2027 bet. |
| Q2 2027 | Khata on UPI | Customer credit ledger with UPI collection and reminders. |
| Q3 2027 | Voice billing in Bengali and Hindi | Speak items at the counter. |
| Q4 2027 | Distributor ordering | Reorder directly from FMCG distributors. |

Risks: a competitor shipping e-invoicing or UPI credit before us removes our differentiation.
""",
    "ideal_customer_profile.md": """# KiranaCloud ideal customer profile

- Independent kirana and general stores in Kolkata and wider West Bengal (78% of revenue).
- 1–3 billing counters, ₹5–40 lakh annual turnover, owner-operated.
- Bengali-speaking owners; many extend informal credit (udhaar) to regular customers.
- Churn risk rises when a competitor is 20%+ cheaper or offers credit/UPI features we lack.
- Sales model: field sales team of 22 in Kolkata, plus WhatsApp onboarding.
""",
}


def _page(title: str, company: story.Company, week: int, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body>
<header><h1>{company.name}</h1><p class="tagline">{html.escape(company.positioning)}</p>
<p class="snapshot">Snapshot for week {week} (from {story.week_start(week).isoformat()})</p></header>
<main>
{body}
</main>
<footer><p>{company.name} · {company.hq}</p></footer>
</body></html>
"""


def _inr(amount: int) -> str:
    return f"₹{amount:,}"


def render_pricing(company: story.Company, week: int) -> str:
    plans = []
    for p in story.pricing(company.name, week):
        price = (
            "Contact sales" if p.price_inr is None else f'{_inr(p.price_inr)}<span class="per">/month</span>'
        )
        was = f' <s class="was">{_inr(p.was_inr)}</s>' if p.was_inr else ""
        feats = "".join(f"<li>{html.escape(f)}</li>" for f in p.features)
        plans.append(
            f'<div class="plan" data-plan="{p.name}">\n  <h2>{p.name}</h2>\n'
            f'  <p class="price">{price}{was}</p>\n  <ul>{feats}</ul>\n</div>'
        )
    return _page(f"{company.name} pricing", company, week, "<h2>Pricing</h2>\n" + "\n".join(plans))


def render_changelog(company: story.Company, week: int) -> str:
    entries = sorted(
        (e for e in story.CHANGELOG[company.name] if e.week <= week), key=lambda e: e.date, reverse=True
    )
    items = "\n".join(
        f'<article class="entry" data-date="{e.date}">\n  <h3>{html.escape(e.title)}</h3>\n'
        f'  <time datetime="{e.date}">{e.date}</time>\n  <p>{html.escape(e.body)}</p>\n</article>'
        for e in entries
    )
    return _page(f"{company.name} changelog", company, week, "<h2>What's new</h2>\n" + items)


def render_careers(company: story.Company, week: int) -> str:
    jobs = "\n".join(
        f'<li class="job" data-location="{j.location}" data-team="{j.team}">'
        f'<h3>{html.escape(j.title)}</h3> <span class="location">{j.location}</span> '
        f'<span class="openings">{j.openings} opening{"s" if j.openings > 1 else ""}</span></li>'
        for j in story.careers(company.name, week)
    )
    return _page(f"{company.name} careers", company, week, f"<h2>Open roles</h2>\n<ul>\n{jobs}\n</ul>")


def write_fakeweb(root: Path) -> None:
    for week in story.WEEKS:
        for co in story.COMPANIES:
            d = root / f"week-{week}" / co.slug
            d.mkdir(parents=True, exist_ok=True)
            (d / "pricing.html").write_text(render_pricing(co, week))
            (d / "changelog.html").write_text(render_changelog(co, week))
            (d / "careers.html").write_text(render_careers(co, week))


def write_news(path: Path) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE articles (id TEXT PRIMARY KEY, company TEXT, week INTEGER, date TEXT, title TEXT, "
            "source TEXT, source_credibility REAL, body TEXT)"
        )
        db.executemany(
            "INSERT INTO articles VALUES (?,?,?,?,?,?,?,?)",
            [(a.id, a.company, a.week, a.date, a.title, a.source, a.credibility, a.body) for a in story.NEWS],
        )
    with sqlite3.connect(path) as db:
        db.execute("VACUUM")  # stable file layout across runs


def write_market(path: Path, rng: random.Random) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE funding_rounds (company TEXT, round TEXT, amount_inr_cr REAL, date TEXT, "
            "week INTEGER, lead_investor TEXT)"
        )
        db.executemany(
            "INSERT INTO funding_rounds VALUES (?,?,?,?,?,?)",
            [(f.company, f.round, f.amount_inr_cr, f.date, f.week, f.lead_investor) for f in story.FUNDING],
        )
        db.execute(
            "CREATE TABLE app_ratings (company TEXT, week INTEGER, rating REAL, review_count INTEGER, "
            "top_complaint TEXT)"
        )
        counts: dict[str, int] = {}
        rows = []
        for r in story.RATINGS:  # review counts grow week over week by a seeded random amount
            counts[r.company] = counts.get(r.company, rng.randint(8_000, 30_000)) + rng.randint(150, 600)
            rows.append((r.company, r.week, r.rating, counts[r.company], r.top_complaint))
        db.executemany("INSERT INTO app_ratings VALUES (?,?,?,?,?)", rows)
    with sqlite3.connect(path) as db:
        db.execute("VACUUM")


def write_ground_truth(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for week, gt in story.GROUND_TRUTH.items():
        (root / f"week-{week}.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n")


def write_knowledge(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in KNOWLEDGE.items():
        (root / name).write_text(text)


def generate(out_root: Path, seed: int = SEED) -> None:
    """Write the whole mock world under `out_root` (the project root by default, a tmp dir in tests)."""
    rng = random.Random(seed)
    data = out_root / "data"
    shutil.rmtree(data / "fakeweb", ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    write_fakeweb(data / "fakeweb")
    write_news(data / "news.sqlite")
    write_market(data / "market.sqlite", rng)
    write_ground_truth(data / "ground_truth")
    write_knowledge(out_root / "knowledge")
