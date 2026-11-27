"""Free web discovery. No API key and no paid search.

DuckDuckGo is tried first. When it answers with a block page, Bing is tried.
Results are the hosts the page actually listed. A blocked or empty page
raises or returns nothing. It never fills the gap with made-up companies.
"""

import logging
import re
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from sellbird.cadence import is_south_africa
from sellbird.collectors.audience import is_coach_search, search_phrase
from sellbird.collectors.discover import (
    Business, DiscoveryProvider, business_name, is_junk, normalize_domain,
)

logger = logging.getLogger("sellbird.discover.web")

_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; Sellbird/0.5; +https://github.com/ethanplusai/sellbird)",
    "Accept-Language": "en",
}
_DIRECTORY = (
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com",
    "wikipedia.org", "reddit.com", "yelp.com", "yellowpages.com",
    "psychologytoday.com", "thumbtack.com", "bark.com", "twitter.com",
    "x.com", "tiktok.com", "pinterest.com", "bing.com", "duckduckgo.com",
    "google.com", "microsoft.com",
)


class WebSearch(DiscoveryProvider):
    """Businesses named by a free search engine result page."""

    key = "ddg"
    label = "Free web search (DuckDuckGo, then Bing)"
    kind = "serp"
    blurb = ("Free web search for practices the map does not list: coaches, "
             "HR advisors, team leaders, and work with individuals and couples. "
             "No key. DuckDuckGo first, Bing when DuckDuckGo blocks the request.")
    cost_note = "free"
    free_tier = "no account"
    signup_url = "https://duckduckgo.com"
    env_keys = ()
    caveat = ("A blocked search fails the query instead of saving an empty "
              "market. Directory sites are left out. Nothing is invented.")

    async def fetch(self, client, env, query):
        phrase = search_phrase(query.term, query.location)
        coach = is_coach_search(query.term) or is_coach_search(phrase)
        errors = []
        saw_page = False
        for name, url, parser in (
            ("duckduckgo", _ddg_url(phrase), _parse_ddg),
            ("bing", _bing_url(phrase), _parse_bing),
        ):
            try:
                response = await client.get(url, headers=_UA, timeout=20.0)
            except httpx.HTTPError as e:
                errors.append(f"{name}: {type(e).__name__}")
                continue
            if response.status_code in (202, 429, 500, 502, 503, 504):
                errors.append(f"{name}: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                errors.append(f"{name}: HTTP {response.status_code}")
                continue
            text = response.text or ""
            if _blocked(text):
                errors.append(f"{name}: blocked this search")
                continue
            hits = parser(text)
            saw_page = True
            kept = _keep(hits, query, coach=coach)
            if kept:
                return kept[: query.limit or 100]
            errors.append(f"{name}: no usable businesses on the page")
        if not saw_page:
            detail = "; ".join(errors) or "no search engine answered"
            raise RuntimeError(
                f"Web search failed for {phrase!r}: {detail}. Nothing was invented."
            )
        logger.info("Web search for %r found no usable businesses (%s).", phrase, "; ".join(errors))
        return []


def _ddg_url(phrase: str) -> str:
    return "https://html.duckduckgo.com/html/?q=" + quote_plus(phrase)


def _bing_url(phrase: str) -> str:
    return "https://www.bing.com/search?q=" + quote_plus(phrase) + "&count=10"


def _blocked(text: str) -> bool:
    low = (text or "").lower()
    return "unfortunately, bots" in low or "anomaly-modal" in low or "captcha" in low[:4000]


def _parse_ddg(html: str) -> list[tuple[str, str, str]]:
    out = []
    for chunk in re.split(r'class="result"', html)[1:]:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"', chunk)
        if not link:
            continue
        href = unescape(link.group(1))
        if "uddg=" in href:
            real = parse_qs(urlparse(href).query).get("uddg") or []
            href = real[0] if real else href
        title = _text(re.search(r'class="result__a"[^>]*>(.*?)</a>', chunk, re.S))
        snippet = _text(re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span|div)>', chunk, re.S))
        out.append((href, title, snippet))
    return out


def _parse_bing(html: str) -> list[tuple[str, str, str]]:
    out = []
    parts = re.split(r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"', html)
    for chunk in parts[1:]:
        title = _text(re.search(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", chunk, re.S))
        cite = re.search(r"<cite[^>]*>(.*?)</cite>", chunk, re.S)
        href = ""
        if cite:
            href = re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", cite.group(1))))
            href = href.split("›")[0].split(">")[0]
        if href and not href.startswith("http"):
            href = "https://" + href.lstrip("/")
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', chunk)
        if link and "bing.com" not in link.group(1):
            href = link.group(1)
        if href:
            out.append((href, title, ""))
    return out


def _text(match) -> str:
    if not match:
        return ""
    raw = re.sub(r"<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", unescape(raw)).strip()


def _directory(domain: str) -> bool:
    host = domain.lower()
    if "directory" in host:
        return True
    return any(host == item or host.endswith("." + item) for item in _DIRECTORY)


def _keep(hits, query, *, coach: bool) -> list[Business]:
    out = []
    seen = set()
    for href, title, snippet in hits:
        domain = normalize_domain(href)
        if not domain or domain in seen or _directory(domain):
            continue
        name = business_name(title, domain) or title
        if is_junk(domain, name or title):
            continue
        if coach and (domain.endswith(".za") or is_south_africa(title, snippet, domain)):
            continue
        seen.add(domain)
        out.append(Business(
            name=name or domain,
            raw_title=title,
            domain=domain,
            website=href if href.startswith("http") else f"https://{domain}",
            location=query.location,
            has_website=True,
            source_url=href if href.startswith("http") else f"https://{domain}",
            provider="ddg",
        ))
    return out
