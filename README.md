# Research News Bot — Electrochemistry & Clean Energy

An automated daily research digest bot that pulls fresh papers and posts from
**arXiv**, **OpenAlex**, **RSS journal feeds**, and **Bluesky**, uses
**Claude** to score relevance and write plain-English summaries, then delivers
a digest to **email**, **Telegram**, and a **dated markdown file committed back
to the repo** — all on a daily **GitHub Actions** cron.

Topics tracked out of the box: water/CO₂ electrolysis, fuel cells, hydrogen,
electrochemical engineering, carbon capture and utilisation. Edit
`config.yaml` to change them.

---

## 1. How it works

```
                    ┌─────────────────────────────────────────┐
                    │            GitHub Actions cron          │
                    │              (07:00 UTC daily)          │
                    └──────────────────┬──────────────────────┘
                                       │
                                       ▼
┌──────────┐  ┌──────────┐  ┌───────┐  ┌──────────┐
│  arXiv   │  │ OpenAlex │  │  RSS  │  │ Bluesky  │   ← src/sources.py
└────┬─────┘  └────┬─────┘  └───┬───┘  └────┬─────┘
     └────────────┬┴────────────┴───────────┘
                  ▼
         ┌──────────────────┐
         │ Claude relevance │   ← src/llm.py
         │ filter + summary │     (Anthropic API)
         └────────┬─────────┘
                  ▼
         ┌──────────────────┐
         │ Digest formatter │   ← src/digest.py
         │   (MD + HTML)    │
         └────────┬─────────┘
                  ▼
   ┌──────────────┼───────────────┐
   ▼              ▼               ▼
 Email         Telegram      digests/YYYY-MM-DD.md
(SMTP)        (Bot API)      (git commit + push)
```

## 2. Quick start

### 2a. Create a GitHub repo

```bash
cd research-news-bot
git init
git add .
git commit -m "Initial commit: research news bot"
gh repo create research-news-bot --private --source=. --push
```

### 2b. Set GitHub Actions secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.

Required — at least one LLM provider (whichever you picked in `config.yaml`):

| Secret | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | From https://console.anthropic.com (for Claude) |
| `DEEPSEEK_API_KEY` | From https://platform.deepseek.com (for DeepSeek) |

You can set both and mix-and-match — e.g. cheap DeepSeek (`deepseek-chat`)
for relevance scoring on every raw item, then sharper Claude Sonnet for the
~20 items that survive the filter. See the `filter:` block in `config.yaml`.

Optional — only set the ones for channels you want to use:

| Secret | Used for |
|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO` | Email delivery |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery |

If a channel's secrets aren't set, the bot just skips it.

### 2c. Run locally to test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in your keys
python -m src.bot --dry-run   # fetches + filters, prints digest, doesn't deliver
python -m src.bot              # full run
```

The first real run typically pulls ~150 raw items, after Claude filtering
you'll see ~10–25 in the digest.

## 3. Configuration

Edit `config.yaml` to change topics, sources, and how many items survive
filtering. Key knobs:

- `topics` — free-text descriptions Claude uses to score relevance. Be
  specific: "alkaline water electrolysis with non-PGM catalysts" filters
  much harder than "electrolysis".
- `arxiv.categories` — arXiv subject classes to query.
- `openalex.search_queries` — OpenAlex full-text queries.
- `rss.feeds` — list of journal/blog feed URLs.
- `bluesky.queries` — search terms for Bluesky public search.
- `max_items_per_source` — fetch cap per source per day.
- `min_relevance_score` — Claude returns 0–10; items below this are dropped.
- `max_digest_items` — hard cap on the final digest length.

## 4. Source coverage notes

| Source | Status | Notes |
|---|---|---|
| arXiv | ✅ Full | Free API, no key. Best categories for this domain: `physics.chem-ph`, `cond-mat.mtrl-sci`, `physics.app-ph` |
| OpenAlex | ✅ Full | Free, no key. Covers most peer-reviewed journals (Nature Energy, JACS, Joule, EES, ACS Energy Letters, …) |
| RSS feeds | ✅ Full | Add any feed URL to `config.yaml`. Pre-populated with a starter set |
| Bluesky | ✅ Read-only | Uses public `app.bsky.feed.searchPosts`; no auth needed for read |
| X / Twitter | ⚠️ Skipped | Requires paid API tier; not worth it for an MVP. Re-add later if you have access |
| LinkedIn | ⚠️ Skipped | No public API for post search; only scraping, which violates ToS |
| WeChat | ⚠️ Skipped | No public API for crawling public-account posts (Sogou search is unreliable and often blocked) |

If you have access to a paid X API or a LinkedIn corporate API key, the
`src/sources.py` file has placeholder functions where you can drop them in.

## 5. Project layout

```
research-news-bot/
├── README.md                     ← you are here
├── requirements.txt
├── config.yaml                   ← topics, sources, thresholds
├── .env.example                  ← copy to .env for local runs
├── .gitignore
├── src/
│   ├── __init__.py
│   ├── bot.py                    ← entrypoint
│   ├── sources.py                ← arXiv / OpenAlex / RSS / Bluesky fetchers
│   ├── llm.py                    ← Claude relevance + summary
│   ├── digest.py                 ← Markdown + HTML formatting
│   └── delivery.py               ← email, Telegram, file-write
├── digests/                      ← daily MD files land here
│   └── .gitkeep
└── .github/workflows/
    └── daily-digest.yml          ← cron + commit-back workflow
```

## 6. LLM provider choice & cost

The bot supports **Anthropic (Claude)** and **DeepSeek** out of the box, and
you can pick a different provider per stage in `config.yaml`:

```yaml
filter:
  scorer:        # runs on every raw item (~150/day) — pick cheap + fast
    provider: "deepseek"
    model: "deepseek-chat"
  summarizer:    # only runs on items that survive (~20/day) — pick sharper
    provider: "anthropic"
    model: "claude-sonnet-4-6"
```

Rough daily cost at default config (~150 raw → ~20 in digest):

| Combo | Scorer | Summarizer | ~$/day |
|---|---|---|---|
| All DeepSeek | `deepseek-chat` | `deepseek-chat` | ~$0.01 |
| Mixed (recommended) | `deepseek-chat` | `claude-sonnet-4-6` | ~$0.05 |
| All Claude (cheap) | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` | ~$0.03 |
| All Claude (sharp) | `claude-haiku-4-5-20251001` | `claude-sonnet-4-6` | ~$0.10 |

So roughly **$0.30–3/month** depending on combo. DeepSeek pricing changes
periodically — check https://api-docs.deepseek.com/quick_start/pricing for
current numbers.

To add another OpenAI-compatible provider (Together, Fireworks, your own
gateway), just point `DEEPSEEK_BASE_URL` at it and use the matching model
name — the wire format is the same.

## 7. Customizing

- **Add a new source**: add a fetcher function to `src/sources.py` returning a
  list of `Item` dicts with `title, authors, url, abstract, source,
  published`, then call it from `fetch_all()`.
- **Change the prompt**: edit `RELEVANCE_PROMPT` / `SUMMARY_PROMPT` in
  `src/llm.py`.
- **Different schedule**: edit the `cron:` line in
  `.github/workflows/daily-digest.yml` (UTC).

## 8. Troubleshooting

- **Action runs but nothing commits** → check the workflow's permissions in
  the repo settings: Settings → Actions → General → Workflow permissions →
  "Read and write permissions".
- **No items pass the filter** → lower `min_relevance_score` in
  `config.yaml`, or broaden `topics`.
- **Email/Telegram silently skipped** → check the workflow run log; missing
  secrets are logged as `[delivery] X channel skipped (missing secrets)`.
