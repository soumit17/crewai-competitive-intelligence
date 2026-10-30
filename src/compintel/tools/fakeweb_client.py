"""Shared fetch helper for our own deterministic tools (not exposed to the LLM).

These tools build the URL themselves from FAKEWEB_URL + (week, company), so the LLM never supplies a URL here.
The LLM-supplied URLs go through crewai's ScrapeWebsiteTool, which the url_allowlist hook polices.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from compintel.world.story import BY_NAME


class FakeWebError(Exception):
    pass


def page_url(company: str, week: int, page: str) -> str:
    from compintel.config import get_settings

    if company not in BY_NAME:
        raise FakeWebError(f"Unknown company {company!r}. Known companies: {', '.join(BY_NAME)}.")
    return f"{get_settings().fakeweb_url}/week-{week}/{BY_NAME[company].slug}/{page}.html"


def fetch(company: str, week: int, page: str) -> str:
    url = page_url(company, week, page)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        raise FakeWebError(f"{url} returned HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise FakeWebError(
            f"Could not reach {url} ({e.reason}). Is `scripts/serve_fakeweb.py` running?"
        ) from e
