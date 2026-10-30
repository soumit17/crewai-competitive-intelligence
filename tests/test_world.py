"""M1: the mock world is deterministic, matches its ground truth, doesn't leak across weeks, and is served."""

import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from compintel.world import story
from compintel.world.fakeweb import make_server
from compintel.world.generate import generate
from compintel.world.market import build_server


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("world")
    generate(root)
    return root


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def week_sources(world, company: str, week: int) -> str:
    """Everything an agent could read about `company` in `week`: pages, that week's news, market data."""
    slug = story.BY_NAME[company].slug
    pages = "".join(
        _text((world / "data/fakeweb" / f"week-{week}" / slug / f"{p}.html").read_text())
        for p in ("pricing", "changelog", "careers")
    )
    with sqlite3.connect(world / "data/news.sqlite") as db:
        news = " ".join(
            f"{t} {b}"
            for t, b in db.execute(
                "SELECT title, body FROM articles WHERE company=? AND week=?", (company, week)
            )
        )
    with sqlite3.connect(world / "data/market.sqlite") as db:
        market = " ".join(
            str(r)
            for r in db.execute(
                "SELECT round, amount_inr_cr FROM funding_rounds WHERE company=? AND week=? "
                "UNION ALL SELECT rating, top_complaint FROM app_ratings WHERE company=? AND week=?",
                (company, week, company, week),
            )
        )
    return f"{pages} {news} {market}"


def test_generator_is_deterministic(world, tmp_path):
    generate(tmp_path)
    for f in sorted(p for p in world.rglob("*") if p.is_file()):
        twin = tmp_path / f.relative_to(world)
        assert twin.read_bytes() == f.read_bytes(), f


def test_ground_truth_files_match_story(world):
    for week in story.WEEKS:
        gt = json.loads((world / f"data/ground_truth/week-{week}.json").read_text())
        assert gt == story.GROUND_TRUTH[week]
    routes = [story.GROUND_TRUTH[w]["expected_route"] for w in story.WEEKS]
    assert routes == ["routine", "material", "routine"]


@pytest.mark.parametrize("week", story.WEEKS)
def test_every_expected_signal_is_findable_in_that_weeks_sources(world, week):
    gt = story.GROUND_TRUTH[week]
    for sig in gt["material_signals"] + gt["must_surface"] + gt["must_discount"]:
        text = week_sources(world, sig["company"], week).lower()
        for needle in sig["detail_contains"]:
            assert needle.lower() in text, (week, sig, needle)


def test_week_one_does_not_leak_later_events(world):
    assert "699" not in week_sources(world, "StockSathi", 1)
    assert "UPI Udhaar" not in week_sources(world, "StockSathi", 1)
    assert "Kolkata" not in week_sources(world, "ShelfMitra", 1)
    assert "e-invoicing" not in week_sources(world, "BahiBook", 2)


def test_rumor_is_low_credibility(world):
    with sqlite3.connect(world / "data/news.sqlite") as db:
        (cred,) = db.execute(
            "SELECT source_credibility FROM articles WHERE title LIKE '%acquired%'"
        ).fetchone()
    assert cred < 0.5


def test_ten_plus_kolkata_sales_roles_from_week_two(world):
    html = (world / "data/fakeweb/week-2/shelfmitra/careers.html").read_text()
    openings = [int(n) for n in re.findall(r'data-location="Kolkata".*?(\d+) opening', html)]
    assert sum(openings) == 15


@pytest.fixture
def fakeweb(world):
    server = make_server(world / "data/fakeweb", port=0)  # port 0 = any free port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_fakeweb_serves_each_week(fakeweb):
    for week, price in ((1, "₹999"), (2, "₹699"), (3, "₹699")):
        with urllib.request.urlopen(f"{fakeweb}/week-{week}/stocksathi/pricing.html") as r:
            assert price in r.read().decode()


@pytest.mark.parametrize("path", ["/week-3/", "/", "/week-1/nope.html"])
def test_fakeweb_hides_listings_and_missing_pages(fakeweb, path):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(fakeweb + path)
    assert e.value.code == 404


async def _call(world, as_of_week: int, tool: str, args: dict):
    server = build_server(world / "data/market.sqlite", as_of_week)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(tool, args)
    text = result.content[0].text
    return json.loads(text) if text.startswith("{") else text


async def test_mcp_lists_both_tools(world):
    async with create_connected_server_and_client_session(build_server(world / "data/market.sqlite", 3)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"funding_rounds", "app_rating_trend"}


async def test_mcp_funding_respects_as_of_week(world):
    week1 = await _call(world, 1, "funding_rounds", {"company": "ShelfMitra"})
    week2 = await _call(world, 2, "funding_rounds", {"company": "ShelfMitra"})
    assert [r["round"] for r in week1["rounds"]] == ["Seed", "Series A"]
    assert week2["rounds"][-1] == {
        "round": "Series B",
        "amount_inr_cr": 200.0,
        "date": story.week_day(2, 3),
        "lead_investor": "Deccan Growth Partners",
        "announced_week": 2,
    }


async def test_mcp_rating_trend_shows_week_three_dip(world):
    trend = await _call(world, 3, "app_rating_trend", {"company": "StockSathi", "weeks": 3})
    assert [r["week"] for r in trend["ratings"]] == [1, 2, 3]
    assert trend["ratings"][-1]["rating"] == 3.9
    assert trend["change_over_period"] == -0.5


async def test_mcp_unknown_company_is_a_helpful_message_not_an_error(world):
    reply = await _call(world, 3, "funding_rounds", {"company": "KhataKing"})
    assert "Unknown company" in reply and "ShelfMitra" in reply
