"""Health check: probe every source and the LLM provider, print a one-page report.

Run from the project root:

    python -m scripts.doctor

Outputs colored ✔ / ✖ / ⚠ for each source so you know immediately what's up
and what's down, plus a suggested `python -m src.bot --dry-run --sources …`
command using only what's working right now.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = "\033[32m✔\033[0m"
BAD = "\033[31m✖\033[0m"
WARN = "\033[33m⚠\033[0m"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
PLAIN_UA = "research-news-bot-doctor/0.1"
TIMEOUT = 15


def probe(url: str, headers: dict | None = None) -> tuple[str, str]:
    """Return (icon, message). Doesn't follow redirects beyond ~5."""
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers or {"User-Agent": UA},
                         timeout=TIMEOUT, allow_redirects=True)
        dt = time.time() - t0
        if r.status_code == 200:
            return OK, f"200 ({dt:.1f}s, {len(r.content)//1024} KiB)"
        return BAD, f"HTTP {r.status_code} ({dt:.1f}s)"
    except requests.exceptions.Timeout:
        return BAD, f"timed out (>{TIMEOUT}s)"
    except requests.exceptions.ConnectionError as e:
        return BAD, f"connection error ({str(e)[:60]})"
    except Exception as e:
        return BAD, f"{type(e).__name__}: {str(e)[:60]}"


def section(title: str) -> None:
    print(f"\n── {title} ───────────────────────────────────────────")


def main() -> int:
    load_dotenv()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())

    print("Research-news-bot doctor — testing every source from this machine.")
    print(f"Working dir: {ROOT}")

    working_sources: list[str] = []

    # ─── arXiv ────────────────────────────────────────────────────────────
    section("arXiv")
    cat = cfg["arxiv"]["categories"][0]
    icon, msg = probe(
        f"https://export.arxiv.org/api/query?search_query=cat:{cat}&max_results=1",
        headers={"User-Agent": PLAIN_UA},
    )
    print(f"  {icon}  {cat}: {msg}")
    if icon == OK:
        working_sources.append("arxiv")

    # ─── OpenAlex ─────────────────────────────────────────────────────────
    section("OpenAlex")
    mailto = os.environ.get("OPENALEX_MAILTO", "")
    suffix = f"&mailto={mailto}" if mailto else ""
    icon, msg = probe(f"https://api.openalex.org/works?per-page=1{suffix}")
    print(f"  {icon}  /works  (mailto={'set' if mailto else 'unset'}): {msg}")
    if icon == OK:
        working_sources.append("openalex")
    elif "503" in msg:
        print(f"  {WARN}  503 = OpenAlex server-side; wait 10–20 min and retry.")

    # ─── Bluesky ──────────────────────────────────────────────────────────
    section("Bluesky")
    icon, msg = probe(
        "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=hydrogen&limit=1"
    )
    print(f"  {icon}  searchPosts: {msg}")
    if icon == OK:
        working_sources.append("bluesky")

    # ─── RSS feeds (one by one) ───────────────────────────────────────────
    section(f"RSS feeds ({len(cfg['rss']['feeds'])} configured)")
    rss_ok = rss_bad = 0
    for feed in cfg["rss"]["feeds"]:
        icon, msg = probe(feed["url"])
        # Compress for readability
        status = msg.split()[0]
        if icon == OK:
            rss_ok += 1
            print(f"  {icon}  {feed['name']:<42}  {status}")
        else:
            rss_bad += 1
            print(f"  {icon}  {feed['name']:<42}  {msg}")
    if rss_ok > 0:
        working_sources.append("rss")
    print(f"\n  Summary: {rss_ok} working, {rss_bad} broken")

    # ─── LLM provider ─────────────────────────────────────────────────────
    section("LLM provider (DeepSeek)")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print(f"  {BAD}  DEEPSEEK_API_KEY not set in .env")
    else:
        try:
            r = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": "deepseek-chat",
                      "messages": [{"role": "user", "content": "say ok"}],
                      "max_tokens": 3},
                timeout=30,
            )
            if r.status_code == 200:
                print(f"  {OK}  DeepSeek API: 200, model reply OK")
            elif r.status_code == 401:
                print(f"  {BAD}  401 Unauthorized — API key is invalid or revoked. "
                      f"Create a new one at platform.deepseek.com")
            elif r.status_code == 402:
                print(f"  {BAD}  402 Payment Required — top up your DeepSeek balance.")
            else:
                print(f"  {BAD}  HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"  {BAD}  {type(e).__name__}: {e}")

    # ─── Verdict & suggested command ──────────────────────────────────────
    section("Verdict")
    if not working_sources:
        print(f"  {BAD}  No sources are reachable. Check your network connection.")
        return 1
    print(f"  {OK}  Working sources: {', '.join(working_sources)}")
    if len(working_sources) < 4:
        print(f"  {WARN}  Some sources are down. Skip them with:")
        print(f"\n     python -m src.bot --dry-run --sources {','.join(working_sources)}\n")
    else:
        print(f"  {OK}  All sources healthy. Run:")
        print(f"\n     python -m src.bot --dry-run\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
