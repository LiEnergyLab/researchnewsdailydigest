"""Claude / DeepSeek calls: score relevance, then summarize the survivors.

Two providers are supported and you can mix-and-match per stage in config.yaml:

    filter:
      scorer:
        provider: deepseek          # 'anthropic' or 'deepseek'
        model: deepseek-chat
      summarizer:
        provider: anthropic
        model: claude-sonnet-4-6

DeepSeek uses an OpenAI-compatible API (base_url=https://api.deepseek.com),
so we hit it directly with `requests` to avoid an extra heavy dependency.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

import requests
from anthropic import Anthropic

log = logging.getLogger("llm")

_anthropic_client: Anthropic | None = None


# ─── Provider abstraction ───────────────────────────────────────────────────
def chat(provider: str, model: str, prompt: str, max_tokens: int = 2000) -> str:
    """Send a single-user-turn prompt and return the assistant text."""
    provider = provider.lower()
    if provider == "anthropic":
        return _anthropic_chat(model, prompt, max_tokens)
    if provider == "deepseek":
        return _deepseek_chat(model, prompt, max_tokens)
    raise ValueError(f"Unknown LLM provider: {provider!r} (expected 'anthropic' or 'deepseek')")


def _anthropic_chat(model: str, prompt: str, max_tokens: int) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = Anthropic(api_key=api_key)
    resp = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _deepseek_chat(model: str, prompt: str, max_tokens: int) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


# ─── Prompts ────────────────────────────────────────────────────────────────
RELEVANCE_PROMPT = """You are a research-news triage assistant for a researcher focused on:

INTERESTED:
{topics}

NOT INTERESTED:
{exclude}

For each numbered item below, output ONE line of strict JSON:
  {{"id": <int>, "score": <0-10 int>, "tag": "<one short tag>"}}

Scoring rubric:
 10 = directly on one of the INTERESTED topics
  7 = closely related, useful for context
  4 = tangential, only worth scanning
  0 = unrelated or in the NOT INTERESTED list

Output the lines in id order, one JSON object per line, nothing else.

Items:
{items}
"""


SUMMARY_PROMPT = """Write a 2–3 sentence plain-English summary of this research item
for a busy electrochemistry/clean-energy researcher. Lead with the concrete
finding or claim. Avoid hype words ("revolutionary", "groundbreaking"). Don't
restate the title. If the input is a social-media post and the claim is thin,
say so in one sentence instead of padding.

Title: {title}
Source: {source}
Authors: {authors}
Abstract / body:
{abstract}

Summary:"""


# ─── Pipeline stages ────────────────────────────────────────────────────────
def score_items(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Add `score` and `tag` fields in-place. Items that fail to parse get score=0."""
    topics = "\n".join(f"- {t}" for t in cfg["topics"])
    exclude = "\n".join(f"- {t}" for t in cfg.get("exclude_topics", [])) or "- (none)"
    scorer = cfg["filter"]["scorer"]
    provider, model = scorer["provider"], scorer["model"]
    batch_size = cfg["filter"]["batch_size"]

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        rendered = "\n\n".join(
            f"[{idx}] {it['title']}\n"
            f"    source: {it['source']}\n"
            f"    abstract: {(it.get('abstract') or '')[:600]}"
            for idx, it in enumerate(batch)
        )
        prompt = RELEVANCE_PROMPT.format(topics=topics, exclude=exclude, items=rendered)
        try:
            raw = chat(provider, model, prompt, max_tokens=2000)
        except Exception as e:
            log.warning("Scorer (%s/%s) call failed on batch %d: %s", provider, model, i, e)
            for it in batch:
                it["score"] = 0
                it["tag"] = "error"
            continue

        parsed = _parse_scores(raw, len(batch))
        for idx, it in enumerate(batch):
            row = parsed.get(idx, {"score": 0, "tag": "unparsed"})
            it["score"] = int(row.get("score", 0))
            it["tag"] = str(row.get("tag", ""))
    return items


def _parse_scores(raw: str, n: int) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            m = re.search(r"\{.*\}", line)
            if not m:
                continue
            line = m.group(0)
        try:
            obj = json.loads(line)
            out[int(obj["id"])] = obj
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    if len(out) < n:
        log.info("Scorer parsed %d/%d items in this batch", len(out), n)
    return out


def summarize_items(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Add a `summary` field. Called only on items that passed the relevance filter."""
    summarizer = cfg["filter"]["summarizer"]
    provider, model = summarizer["provider"], summarizer["model"]
    for it in items:
        prompt = SUMMARY_PROMPT.format(
            title=it.get("title", ""),
            source=it.get("source", ""),
            authors=it.get("authors", "") or "—",
            abstract=(it.get("abstract") or "")[:2000] or "(no abstract available)",
        )
        try:
            it["summary"] = chat(provider, model, prompt, max_tokens=300).strip()
        except Exception as e:
            log.warning("Summary (%s/%s) failed for %r: %s",
                        provider, model, it.get("title", "")[:60], e)
            it["summary"] = (it.get("abstract") or "")[:300]
    return items
