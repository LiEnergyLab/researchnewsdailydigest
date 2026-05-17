"""Source fetchers.

Every fetcher returns a list of dicts with the same shape:

    {
        "title":     str,
        "authors":   str,        # comma-joined
        "url":       str,
        "abstract":  str,        # may be empty for some sources
        "source":    str,        # e.g. "arXiv", "OpenAlex", "RSS:Nature Energy"
        "published": str,        # ISO date (YYYY-MM-DD) when available
    }
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Dict, List

import feedparser
import requests
from dateutil import parser as dateparser

log = logging.getLogger("sources")

Item = Dict[str, Any]

REQUEST_TIMEOUT = 30
# A browser-like UA dodges Cloudflare blocks on Nature / Cell / RSC / Wiley
# RSS feeds. arXiv and OpenAlex also accept it (OpenAlex prefers a mailto
# query-param for the "polite pool" — see fetch_openalex below).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
RSS_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


# ─── arXiv ──────────────────────────────────────────────────────────────────
# arXiv rate-limits aggressively. Use requests with URL-encoded params,
# a 3-second gap between calls (per arXiv's API guidance), and a plain UA —
# they sometimes flake on browser-impersonating UAs.
ARXIV_HEADERS = {"User-Agent": "research-news-bot/0.1 (mailto:contact@example.com)"}


def fetch_arxiv(categories: List[str], max_results: int, days_back: int) -> List[Item]:
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=days_back)
    items: List[Item] = []
    for idx, cat in enumerate(categories):
        if idx > 0:
            time.sleep(3)  # arXiv asks for ≥3s between calls
        query = urllib.parse.urlencode({
            "search_query": f"cat:{cat}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        })
        url = f"https://export.arxiv.org/api/query?{query}"
        r = None
        for attempt in (1, 2):
            try:
                r = requests.get(url, headers=ARXIV_HEADERS, timeout=60)
                if r.status_code == 429:
                    # arXiv is rate-limiting us — respect Retry-After if present,
                    # otherwise back off 30s (their typical cooldown).
                    wait = int(r.headers.get("Retry-After", "30"))
                    log.info("arXiv 429 for %s — sleeping %ds before retry", cat, wait)
                    time.sleep(wait)
                    r = None
                    continue
                r.raise_for_status()
                break
            except Exception as e:
                log.info("arXiv attempt %d failed for %s: %s", attempt, cat, e)
                r = None
                if attempt == 1:
                    time.sleep(5)
        if r is None:
            log.warning("arXiv fetch failed for %s (after retries)", cat)
            continue
        feed = feedparser.parse(r.content)

        log.info("arXiv:%s raw entries: %d", cat, len(feed.entries))
        for e in feed.entries:
            published = _safe_date(getattr(e, "published", ""))
            if published and published < cutoff:
                continue
            items.append(
                {
                    "title": _clean(e.get("title", "")),
                    "authors": ", ".join(a.name for a in e.get("authors", [])),
                    "url": e.get("link", ""),
                    "abstract": _clean(e.get("summary", "")),
                    "source": f"arXiv:{cat}",
                    "published": published.date().isoformat() if published else "",
                }
            )
    log.info("arXiv: %d items", len(items))
    return items


# ─── OpenAlex ───────────────────────────────────────────────────────────────
def fetch_openalex(queries: List[str], max_results_per_query: int, days_back: int) -> List[Item]:
    cutoff = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    items: List[Item] = []
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    for q in queries:
        params = {
            "search": q,
            "filter": f"from_publication_date:{cutoff}",
            "per-page": max_results_per_query,
            "sort": "publication_date:desc",
        }
        if mailto:
            params["mailto"] = mailto   # "polite pool" — faster + more reliable
        r = _openalex_get("https://api.openalex.org/works", params)
        if r is None:
            log.warning("OpenAlex fetch failed for %r (after retries)", q)
            continue

        for w in r.json().get("results", []):
            abstract = _reconstruct_openalex_abstract(w.get("abstract_inverted_index"))
            items.append(
                {
                    "title": _clean(w.get("title") or ""),
                    "authors": ", ".join(
                        a.get("author", {}).get("display_name", "")
                        for a in (w.get("authorships") or [])[:6]
                    ),
                    "url": (w.get("primary_location") or {}).get("landing_page_url")
                    or w.get("doi")
                    or w.get("id", ""),
                    "abstract": abstract,
                    "source": "OpenAlex",
                    "published": w.get("publication_date", ""),
                }
            )
    log.info("OpenAlex: %d items", len(items))
    return items


# ─── OpenAlex: tracked authors ──────────────────────────────────────────────
def fetch_openalex_authors(authors: List[Dict[str, str]], days_back: int, max_per_author: int = 10) -> List[Item]:
    """Pull recent works for a curated list of researchers/groups.

    `authors` is a list of {name, id?, orcid?} dicts. Either `id` (OpenAlex
    author ID like 'A5012345') or `orcid` is preferred — name-only lookup
    works but can be ambiguous.
    """
    if not authors:
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    items: List[Item] = []
    for a in authors:
        author_id = _resolve_openalex_author(a, mailto)
        if not author_id:
            log.warning("Could not resolve OpenAlex author: %s", a.get("name"))
            continue
        params = {
            "filter": f"author.id:{author_id},from_publication_date:{cutoff}",
            "sort": "publication_date:desc",
            "per-page": max_per_author,
        }
        if mailto:
            params["mailto"] = mailto
        r = _openalex_get("https://api.openalex.org/works", params)
        if r is None:
            log.warning("Author works fetch failed for %s (after retries)", a.get("name"))
            continue
        for w in r.json().get("results", []):
            items.append(
                {
                    "title": _clean(w.get("title") or ""),
                    "authors": ", ".join(
                        au.get("author", {}).get("display_name", "")
                        for au in (w.get("authorships") or [])[:6]
                    ),
                    "url": (w.get("primary_location") or {}).get("landing_page_url")
                    or w.get("doi") or w.get("id", ""),
                    "abstract": _reconstruct_openalex_abstract(w.get("abstract_inverted_index")),
                    "source": f"Author:{a.get('name')}",
                    "published": w.get("publication_date", ""),
                }
            )
    log.info("OpenAlex-authors: %d items", len(items))
    return items


def _openalex_get(url: str, params: Dict[str, Any]):
    """GET with longer timeout + one retry. Returns the Response or None."""
    for attempt in (1, 2):
        try:
            r = requests.get(
                url, params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            r.raise_for_status()
            return r
        except Exception as e:
            log.info("OpenAlex GET attempt %d failed (%s): %s", attempt, url, e)
            if attempt == 1:
                time.sleep(2)
    return None


def _resolve_openalex_author(a: Dict[str, str], mailto: str) -> str:
    """Return an OpenAlex author ID. Cheapest path = use the one in config."""
    if a.get("id"):
        return a["id"] if a["id"].startswith("A") else a["id"].rsplit("/", 1)[-1]
    if a.get("orcid"):
        params: Dict[str, Any] = {"filter": f"orcid:{a['orcid']}"}
        if mailto:
            params["mailto"] = mailto
        r = _openalex_get("https://api.openalex.org/authors", params)
        if r is not None:
            results = r.json().get("results", [])
            if results:
                return results[0]["id"].rsplit("/", 1)[-1]
    if a.get("name"):
        params = {"search": a["name"], "per-page": 1}
        if mailto:
            params["mailto"] = mailto
        r = _openalex_get("https://api.openalex.org/authors", params)
        if r is not None:
            results = r.json().get("results", [])
            if results:
                return results[0]["id"].rsplit("/", 1)[-1]
    return ""


def _reconstruct_openalex_abstract(inv: Dict[str, List[int]] | None) -> str:
    """OpenAlex returns abstracts as an inverted index. Rebuild the text."""
    if not inv:
        return ""
    positions: Dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


# ─── RSS ────────────────────────────────────────────────────────────────────
def fetch_rss(feeds: List[Dict[str, str]], max_items_per_feed: int) -> List[Item]:
    items: List[Item] = []
    for feed_cfg in feeds:
        name, url = feed_cfg["name"], feed_cfg["url"]
        try:
            # Fetch via requests with browser headers (defeats Cloudflare on
            # Nature/ACS/Wiley/RSC), then hand bytes to feedparser.
            r = requests.get(url, headers=RSS_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            if feed.bozo and not feed.entries:
                log.warning("RSS parse warning for %s: %s", name, getattr(feed, "bozo_exception", "?"))
                continue
        except Exception as e:
            log.warning("RSS fetch failed for %s: %s", name, e)
            continue

        if not feed.entries:
            log.info("RSS feed %s returned 0 entries (URL may have changed)", name)

        for e in feed.entries[:max_items_per_feed]:
            published = _safe_date(
                e.get("published", "") or e.get("updated", "") or ""
            )
            items.append(
                {
                    "title": _clean(e.get("title", "")),
                    "authors": _clean(e.get("author", "")),
                    "url": e.get("link", ""),
                    "abstract": _clean(
                        e.get("summary", "") or e.get("description", "")
                    ),
                    "source": f"RSS:{name}",
                    "published": published.date().isoformat() if published else "",
                }
            )
    log.info("RSS: %d items", len(items))
    return items


# ─── Bluesky ────────────────────────────────────────────────────────────────
def fetch_bluesky(queries: List[str], max_results_per_query: int) -> List[Item]:
    """Public read-only search via the AppView API. No auth needed for read."""
    items: List[Item] = []
    for q in queries:
        try:
            r = requests.get(
                "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                params={"q": q, "limit": max_results_per_query, "sort": "latest"},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("Bluesky fetch failed for %r: %s", q, e)
            continue

        for p in r.json().get("posts", []):
            record = p.get("record", {}) or {}
            text = record.get("text", "") or ""
            handle = (p.get("author") or {}).get("handle", "")
            rkey = (p.get("uri", "") or "").rsplit("/", 1)[-1]
            url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""
            items.append(
                {
                    "title": _clean(text[:140]),
                    "authors": handle,
                    "url": url,
                    "abstract": _clean(text),
                    "source": f"Bluesky:{q}",
                    "published": (record.get("createdAt") or "")[:10],
                }
            )
    log.info("Bluesky: %d items", len(items))
    return items


# ─── Stubs for later (paid / restricted APIs) ───────────────────────────────
def fetch_twitter(*_args, **_kw) -> List[Item]:
    """X/Twitter requires a paid API tier. Drop your client in here when ready."""
    return []


def fetch_linkedin(*_args, **_kw) -> List[Item]:
    """LinkedIn has no public post search API. Skip unless you have a partner key."""
    return []


def fetch_wechat(*_args, **_kw) -> List[Item]:
    """WeChat public-account posts have no stable public API. Skip."""
    return []


# ─── Orchestrator ───────────────────────────────────────────────────────────
def fetch_all(cfg: Dict[str, Any]) -> List[Item]:
    """Fetch from every enabled source, dedupe by URL+title, return the bag."""
    items: List[Item] = []
    if cfg.get("arxiv", {}).get("enabled"):
        c = cfg["arxiv"]
        items += fetch_arxiv(c["categories"], c["max_results"], c["days_back"])
    if cfg.get("openalex", {}).get("enabled"):
        c = cfg["openalex"]
        items += fetch_openalex(c["search_queries"], c["max_results_per_query"], c["days_back"])
        tracked = c.get("tracked_authors") or []
        if tracked:
            items += fetch_openalex_authors(tracked, c.get("authors_days_back", 14),
                                            c.get("max_works_per_author", 10))
    if cfg.get("rss", {}).get("enabled"):
        c = cfg["rss"]
        items += fetch_rss(c["feeds"], c["max_items_per_feed"])
    if cfg.get("bluesky", {}).get("enabled"):
        c = cfg["bluesky"]
        items += fetch_bluesky(c["queries"], c["max_results_per_query"])
    return _dedupe(items)


def _dedupe(items: List[Item]) -> List[Item]:
    seen = set()
    out = []
    for it in items:
        key = (it.get("url", "").strip().lower(), it.get("title", "").strip().lower())
        if key in seen or not key[1]:
            continue
        seen.add(key)
        out.append(it)
    log.info("After dedupe: %d items", len(out))
    return out


# ─── helpers ────────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")        # strip HTML tags
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _safe_date(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dateparser.parse(s)
        if d.tzinfo is not None:
            d = d.replace(tzinfo=None)
        return d
    except (ValueError, TypeError):
        return None
