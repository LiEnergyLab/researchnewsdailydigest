"""Build the Markdown + HTML digest from scored items."""

from __future__ import annotations

import datetime as dt
import html
from collections import defaultdict
from typing import Any, Dict, List


def build_markdown(items: List[Dict[str, Any]], today: str | None = None) -> str:
    today = today or dt.date.today().isoformat()
    n = len(items)
    lines: List[str] = [
        f"# Research Digest — {today}",
        "",
        f"_{n} item{'s' if n != 1 else ''} after filtering._",
        "",
    ]
    for source, group in _group_by_source(items).items():
        lines.append(f"## {source}")
        lines.append("")
        for it in group:
            title = it.get("title", "(no title)").strip()
            url = it.get("url", "")
            authors = it.get("authors", "") or "—"
            published = it.get("published", "") or ""
            score = it.get("score", "")
            tag = it.get("tag", "")
            summary = (it.get("summary") or "").strip() or (it.get("abstract") or "")[:300]

            lines.append(f"### [{title}]({url})" if url else f"### {title}")
            meta_bits = [f"**Score:** {score}/10"]
            if tag:
                meta_bits.append(f"**Tag:** {tag}")
            if published:
                meta_bits.append(f"**Published:** {published}")
            if authors and authors != "—":
                meta_bits.append(f"**Authors:** {authors}")
            lines.append(" · ".join(meta_bits))
            lines.append("")
            lines.append(summary)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_html(items: List[Dict[str, Any]], today: str | None = None) -> str:
    today = today or dt.date.today().isoformat()
    n = len(items)
    parts: List[str] = [
        "<!doctype html><html><body style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:24px auto;color:#222'>",
        f"<h1 style='margin-bottom:0'>Research Digest — {today}</h1>",
        f"<p style='color:#666'>{n} item{'s' if n != 1 else ''} after filtering.</p>",
    ]
    for source, group in _group_by_source(items).items():
        parts.append(f"<h2 style='border-bottom:1px solid #eee;padding-bottom:4px'>{html.escape(source)}</h2>")
        for it in group:
            title = html.escape(it.get("title", "(no title)").strip())
            url = html.escape(it.get("url", ""), quote=True)
            authors = html.escape(it.get("authors", "") or "")
            published = html.escape(it.get("published", "") or "")
            score = it.get("score", "")
            tag = html.escape(it.get("tag", "") or "")
            summary = html.escape((it.get("summary") or "").strip() or (it.get("abstract") or "")[:300])

            title_html = f"<a href='{url}' style='color:#0a58ca;text-decoration:none'>{title}</a>" if url else title
            meta = f"<span style='color:#888;font-size:12px'>Score {score}/10"
            if tag:
                meta += f" · {tag}"
            if published:
                meta += f" · {published}"
            if authors:
                meta += f" · {authors}"
            meta += "</span>"

            parts.append(
                f"<div style='margin:14px 0 18px'>"
                f"<div style='font-weight:600;font-size:15px'>{title_html}</div>"
                f"<div>{meta}</div>"
                f"<div style='margin-top:6px'>{summary}</div>"
                f"</div>"
            )
    parts.append("</body></html>")
    return "".join(parts)


def build_telegram_text(items: List[Dict[str, Any]], today: str | None = None, max_chars: int = 3800) -> str:
    """Telegram messages are capped at 4096 chars. Keep it compact."""
    today = today or dt.date.today().isoformat()
    lines: List[str] = [f"📚 Research digest — {today}  ({len(items)} items)", ""]
    for it in items:
        title = it.get("title", "(no title)").strip()
        url = it.get("url", "")
        score = it.get("score", "")
        summary = (it.get("summary") or "").strip()
        block = f"• [{score}/10] {title}\n{url}\n{summary[:240]}".rstrip()
        if sum(len(x) for x in lines) + len(block) > max_chars:
            lines.append("…(truncated — see email or repo for full digest)")
            break
        lines.append(block)
        lines.append("")
    return "\n".join(lines)


def _group_by_source(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    # Collapse "arXiv:physics.chem-ph" → "arXiv", "RSS:Nature Energy" → "Nature Energy", etc.
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        src = it.get("source", "other")
        if src.startswith("arXiv"):
            label = "arXiv"
        elif src.startswith("RSS:"):
            label = src.split(":", 1)[1]
        elif src.startswith("Bluesky"):
            label = "Bluesky"
        else:
            label = src
        groups[label].append(it)
    # Sort items within each group by score desc
    for k in groups:
        groups[k].sort(key=lambda x: x.get("score", 0), reverse=True)
    # Sort groups: peer-reviewed first, arXiv next, Bluesky last
    def group_rank(name: str) -> int:
        if name == "Bluesky":
            return 9
        if name == "arXiv":
            return 5
        if name == "OpenAlex":
            return 2
        return 1
    return dict(sorted(groups.items(), key=lambda kv: (group_rank(kv[0]), kv[0].lower())))
