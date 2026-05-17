"""Entrypoint:  python -m src.bot  [--dry-run] [--config path/to/config.yaml]

Pipeline:  fetch  →  Claude/DeepSeek score  →  filter  →  summarize  →  deliver.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import delivery, digest, llm, sources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-10s  %(levelname)s  %(message)s",
)
log = logging.getLogger("bot")

_FUNDING_RE = re.compile(
    r"\bfund|\bpolic(?:y|ies)?\b|\bgrant\b|\bgovernment\b"
    r"|\bDOE\b|\bARENA\b|\bARPA\b|\bNSFC\b|\bMoST\b|\bHorizon\b|\bsubsid",
    re.IGNORECASE,
)


def _is_funding(it: dict) -> bool:
    return any(_FUNDING_RE.search(it.get(f) or "") for f in ("tag", "source", "title"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + filter + format, but don't send email/Telegram or write the file.",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Comma-separated subset of sources to enable (e.g. 'rss,bluesky'). "
             "Useful when arXiv or OpenAlex is having an outage. "
             "Choices: arxiv, openalex, rss, bluesky. Default = use whatever is "
             "enabled in config.yaml.",
    )
    args = parser.parse_args(argv)

    load_dotenv()  # picks up .env for local runs; harmless in CI

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error("Config not found: %s", cfg_path)
        return 2
    cfg = yaml.safe_load(cfg_path.read_text())

    # Apply --sources override: temporarily flip enabled flags
    if args.sources:
        wanted = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
        valid = {"arxiv", "openalex", "rss", "bluesky"}
        unknown = wanted - valid
        if unknown:
            log.error("Unknown --sources: %s (valid: %s)", unknown, valid)
            return 2
        for src in valid:
            cfg.setdefault(src, {})["enabled"] = src in wanted
        log.info("--sources override: enabled = %s", sorted(wanted))

    # 1. Fetch
    log.info("=== Fetching ===")
    items = sources.fetch_all(cfg)
    if not items:
        log.warning("No items fetched. Exiting.")
        return 0

    # 2. Score with the cheap model
    log.info("=== Scoring %d items ===", len(items))
    items = llm.score_items(items, cfg)

    # 3. Filter + cap
    min_score = cfg["filter"]["min_relevance_score"]
    max_items = cfg["filter"]["max_digest_items"]
    survivors = sorted(
        [it for it in items if it.get("score", 0) >= min_score],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:max_items]
    log.info("=== %d items above score %d (cap %d) ===", len(survivors), min_score, max_items)

    if not survivors:
        log.warning("No items survived the relevance filter — nothing to send.")
        return 0

    # 3a. Guarantee a minimum number of funding/policy items.
    # If the main filter left us short, pull the best-scoring funding items
    # from the full scored pool (score >= 2 to avoid truly irrelevant noise).
    min_funding = cfg["filter"].get("min_funding_items", 5)
    n_funding = sum(1 for it in survivors if _is_funding(it))
    if n_funding < min_funding:
        survivor_urls = {it.get("url", "") for it in survivors}
        rescue = sorted(
            [it for it in items
             if _is_funding(it)
             and it.get("url", "") not in survivor_urls
             and it.get("score", 0) >= 2],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )[:min_funding - n_funding]
        survivors.extend(rescue)
        if rescue:
            log.info("Funding quota: added %d item(s) (funding total: %d)",
                     len(rescue), n_funding + len(rescue))

    # 4. Summarize the survivors with the sharper model
    log.info("=== Summarizing ===")
    survivors = llm.summarize_items(survivors, cfg)

    # 5. Format
    today = dt.date.today().isoformat()
    md = digest.build_markdown(survivors, today=today)
    html_body = digest.build_html(survivors, today=today)
    tg = digest.build_telegram_text(survivors, today=today)

    if args.dry_run:
        log.info("=== DRY RUN — digest preview ===")
        print(md)
        return 0

    # 6. Deliver
    log.info("=== Delivering ===")
    d = cfg.get("delivery", {})
    if d.get("github_commit"):
        digest_path = delivery.write_markdown_file(md, date=today)
        delivery.commit_and_push_markdown(digest_path)
    if d.get("email"):
        delivery.send_email(
            subject=f"Research Digest — {today} ({len(survivors)} items)",
            html_body=html_body,
            text_body=md,
        )
    if d.get("telegram"):
        delivery.send_telegram(tg)

    log.info("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
