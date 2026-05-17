"""Automated pipeline diagnostic: health check + dry-run + one-page report.

Run:
    python -m scripts.diagnose

What it does:
  1. Probes every source endpoint (same checks as scripts.doctor).
  2. Fetches from healthy sources only, scores with the LLM.
  3. Prints a consolidated report:
       • per-source item counts
       • score-threshold line
       • bad ORCIDs (tracked authors that couldn't be resolved)
       • RSS feeds that returned 0 entries (HTTP-OK but likely dead)
       • 3–5 sample digest entries
  4. Exits non-zero so CI can catch regressions.

Exit codes:
  0  — everything looks healthy
  1  — warnings: bad ORCIDs and/or empty RSS feeds (but pipeline ran OK)
  2  — critical: no working sources, or zero items survived the score filter
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.doctor import OK, BAD, WARN, PLAIN_UA, probe, section  # noqa: E402
from src import llm, sources  # noqa: E402


# ─── log capture ─────────────────────────────────────────────────────────────


class _LogCapture(logging.Handler):
    """Accumulate log records from a named logger for post-run analysis."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ─── health checks ───────────────────────────────────────────────────────────


def _check_sources(cfg: dict) -> tuple[list[str], list[str]]:
    """Probe every source endpoint.

    Returns:
        working   — source names that answered OK (subset of arxiv/openalex/rss/bluesky)
        dead_rss  — RSS feed names that failed the HTTP probe
    """
    working: list[str] = []

    cat = cfg["arxiv"]["categories"][0]
    icon, msg = probe(
        f"https://export.arxiv.org/api/query?search_query=cat:{cat}&max_results=1",
        headers={"User-Agent": PLAIN_UA},
    )
    print(f"  {icon}  arXiv ({cat}): {msg}")
    if icon == OK:
        working.append("arxiv")

    mailto = os.environ.get("OPENALEX_MAILTO", "")
    suffix = f"&mailto={mailto}" if mailto else ""
    icon, msg = probe(f"https://api.openalex.org/works?per-page=1{suffix}")
    print(f"  {icon}  OpenAlex: {msg}")
    if icon == OK:
        working.append("openalex")

    icon, msg = probe(
        "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=hydrogen&limit=1"
    )
    print(f"  {icon}  Bluesky: {msg}")
    if icon == OK:
        working.append("bluesky")

    dead_rss: list[str] = []
    rss_ok = 0
    for feed in cfg["rss"]["feeds"]:
        f_icon, _ = probe(feed["url"])
        if f_icon == OK:
            rss_ok += 1
        else:
            dead_rss.append(feed["name"])

    total_rss = len(cfg["rss"]["feeds"])
    rss_icon = OK if rss_ok > 0 else BAD
    suffix = f"  ({len(dead_rss)} HTTP failures)" if dead_rss else ""
    print(f"  {rss_icon}  RSS feeds: {rss_ok}/{total_rss} reachable{suffix}")
    if rss_ok > 0:
        working.append("rss")

    return working, dead_rss


# ─── pipeline ────────────────────────────────────────────────────────────────


def _run_pipeline(cfg: dict, working: list[str]) -> tuple[dict, list[dict]]:
    """Fetch → dedupe → score → filter.

    Calls each source fetcher individually (instead of sources.fetch_all) so
    we can record per-source counts before deduplication. A capturing log
    handler intercepts ORCID warnings and empty-feed notices from sources.py.

    Returns:
        counts    — dict with per-source counts and diagnostic metadata
        survivors — items that passed the relevance filter, sorted by score desc
    """
    capture = _LogCapture()
    src_log = logging.getLogger("sources")
    src_log.addHandler(capture)

    counts: dict = {}
    all_items: list[dict] = []

    try:
        if "arxiv" in working:
            c = cfg["arxiv"]
            fetched = sources.fetch_arxiv(c["categories"], c["max_results"], c["days_back"])
            counts["arXiv"] = len(fetched)
            all_items.extend(fetched)

        if "openalex" in working:
            c = cfg["openalex"]
            fetched = sources.fetch_openalex(
                c["search_queries"], c["max_results_per_query"], c["days_back"]
            )
            counts["OpenAlex"] = len(fetched)
            all_items.extend(fetched)

            tracked = c.get("tracked_authors") or []
            if tracked:
                fetched = sources.fetch_openalex_authors(
                    tracked,
                    c.get("authors_days_back", 14),
                    c.get("max_works_per_author", 10),
                )
                counts["OpenAlex-authors"] = len(fetched)
                all_items.extend(fetched)

        if "rss" in working:
            c = cfg["rss"]
            fetched = sources.fetch_rss(c["feeds"], c["max_items_per_feed"])
            counts["RSS"] = len(fetched)
            all_items.extend(fetched)

        if "bluesky" in working:
            c = cfg["bluesky"]
            fetched = sources.fetch_bluesky(c["queries"], c["max_results_per_query"])
            counts["Bluesky"] = len(fetched)
            all_items.extend(fetched)

        deduped = sources._dedupe(all_items)
        counts["After dedupe"] = len(deduped)

        scored = llm.score_items(deduped, cfg) if deduped else []

    finally:
        src_log.removeHandler(capture)

    min_score: int = cfg["filter"]["min_relevance_score"]
    max_items: int = cfg["filter"]["max_digest_items"]
    survivors = sorted(
        [it for it in scored if it.get("score", 0) >= min_score],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:max_items]

    counts["_min_score"] = min_score
    counts["_max_items"] = max_items
    counts["_above_threshold"] = len(survivors)

    # Extract warnings from captured log records
    bad_orcids: list[str] = []
    empty_rss: list[str] = []
    for rec in capture.records:
        msg = rec.getMessage()
        if "Could not resolve OpenAlex author:" in msg:
            name = msg.split("Could not resolve OpenAlex author:", 1)[-1].strip()
            bad_orcids.append(name)
        elif "returned 0 entries" in msg:
            feed_name = msg.split("RSS feed", 1)[-1].split("returned 0 entries")[0].strip()
            empty_rss.append(feed_name)

    counts["_bad_orcids"] = bad_orcids
    counts["_empty_rss"] = empty_rss

    return counts, survivors


# ─── report ──────────────────────────────────────────────────────────────────


def _print_report(
    counts: dict,
    survivors: list[dict],
    dead_rss_http: list[str],
) -> int:
    """Print the consolidated diagnostic report. Return exit code."""
    min_score: int = counts.get("_min_score", 6)
    max_items: int = counts.get("_max_items", "?")  # type: ignore[assignment]
    above: int = counts.get("_above_threshold", 0)
    bad_orcids: list[str] = counts.get("_bad_orcids", [])
    empty_rss: list[str] = counts.get("_empty_rss", [])

    section("Pipeline report")

    print("\n  Source counts:")
    display_order = ("arXiv", "OpenAlex", "OpenAlex-authors", "RSS", "Bluesky")
    for label in display_order:
        if label in counts:
            print(f"    {label:<20}  {counts[label]:>5} items")
    if "After dedupe" in counts:
        print(f"    {'─' * 28}")
        print(f"    {'After dedupe':<20}  {counts['After dedupe']:>5} items")

    print(f"\n  Score ≥ {min_score}/10  →  {above} items survive (cap {max_items})")

    if bad_orcids:
        print(f"\n  {WARN}  Unresolvable ORCIDs ({len(bad_orcids)}) — check config.yaml tracked_authors:")
        for name in bad_orcids:
            print(f"       - {name}")

    # Merge HTTP-dead and fetch-empty RSS problems into one list (no duplicates)
    empty_rss_new = [f for f in empty_rss if f not in dead_rss_http]
    all_dead_rss = dead_rss_http + empty_rss_new
    if all_dead_rss:
        print(f"\n  {WARN}  Problematic RSS feeds ({len(all_dead_rss)}):")
        for name in dead_rss_http:
            print(f"       - {name}  (HTTP failure)")
        for name in empty_rss_new:
            print(f"       - {name}  (0 entries — URL may have changed)")

    if survivors:
        sample = survivors[:5]
        section(f"Sample digest ({len(sample)} of {above} items)")
        for i, it in enumerate(sample, 1):
            score = it.get("score", "?")
            src = it.get("source", "?")
            pub = it.get("published", "")
            title = (it.get("title") or "(no title)")[:80]
            url = it.get("url", "")
            abstract = (it.get("abstract") or "")[:200].replace("\n", " ")
            print(f"\n  [{i}] {score}/10  ·  {src}  ·  {pub}")
            print(f"      {title}")
            if abstract:
                print(f"      {abstract}")
            if url:
                print(f"      {url}")

    section("Verdict")
    exit_code = 0

    if above == 0:
        print(f"  {BAD}  Zero items above score threshold — check LLM config or topic list")
        exit_code = 2
    else:
        print(f"  {OK}  {above} items survived score ≥ {min_score}")

    if bad_orcids:
        print(f"  {WARN}  {len(bad_orcids)} bad ORCID(s) — fix in config.yaml tracked_authors")
        exit_code = max(exit_code, 1)

    if all_dead_rss:
        print(f"  {WARN}  {len(all_dead_rss)} broken RSS feed(s) — review feed URLs")
        exit_code = max(exit_code, 1)

    if exit_code == 0:
        print(f"  {OK}  All checks passed")

    print()
    return exit_code


# ─── entry point ─────────────────────────────────────────────────────────────


def main() -> int:
    load_dotenv()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())

    print("Research-news-bot diagnose — health check + pipeline dry-run")
    print(f"Config: {ROOT / 'config.yaml'}")

    section("Health checks")
    working, dead_rss_http = _check_sources(cfg)

    if not working:
        print(f"\n  {BAD}  No sources reachable. Check your network connection.")
        return 2

    print(f"\n  Working sources: {', '.join(working)}")

    # Restrict config to healthy sources only — same mechanic as bot.py --sources
    for src in {"arxiv", "openalex", "rss", "bluesky"}:
        cfg.setdefault(src, {})["enabled"] = src in working

    section("Fetching + scoring  (may take a few minutes)")
    counts, survivors = _run_pipeline(cfg, working)

    return _print_report(counts, survivors, dead_rss_http)


if __name__ == "__main__":
    sys.exit(main())
