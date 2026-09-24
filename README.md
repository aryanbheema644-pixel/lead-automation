# LeadSearch — Lead Verification Pipeline

Turn a messy spreadsheet of leads into verified, enriched person profiles — with
sources, a confidence score, and a human-review lane for uncertain matches.

```
CSV upload → extract & enrich (LLM) → generate queries (LLM) → search (Tavily)
   → classify → get content (Tavily first, Firecrawl/Apify fallback)
   → match & score (LLM) → decide → store → review uncertain → export
```

## The fallback logic (the key design choice)

Tavily returns page content in the same call as search, so we **use Tavily's content
directly when it's substantial enough**. We only reach for a second fetch when needed:

- **LinkedIn URLs** → Apify actor (if configured)
- **PDFs / heavy-JS pages / thin content** → Firecrawl
- **everything else** → Tavily's `raw_content`, no extra fetch

## Setup (one time)

### 1. Python environment

```bash
cd /Users/aryanbheema/leadsearch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. API keys

```bash
cp .env.example .env
```

Then open `.env` and fill in the keys. You can start with just **OpenRouter + Tavily**
and add Firecrawl/Apify later — the app degrades gracefully.

| Key | Where to get it | Needed for |
|-----|-----------------|-----------|
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys | LLM steps (required) |
| `OPENROUTER_MODEL`   | https://openrouter.ai/models | pick your model here |
| `TAVILY_API_KEY`     | https://app.tavily.com | web search (required) |
| `FIRECRAWL_API_KEY`  | https://www.firecrawl.dev | fallback fetch (optional) |
| `APIFY_API_TOKEN`    | https://console.apify.com/account/integrations | LinkedIn (optional) |
| `APIFY_LINKEDIN_ACTOR` | Apify Store, e.g. `username~actor-name` | LinkedIn (optional) |

### 3. Run it

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000**.

## Using it

1. Drag `data/sample_leads.csv` onto the upload box (or your own CSV).
2. Click **Run pipeline**. Watch statuses update live.
3. **Accepted** leads are auto-verified; **Needs review** leads show the top candidates
   with source links, match signals, and per-candidate scores — click **Details** to
   pick the right one (this is the "Fred" review step).
4. **Export verified CSV** downloads the accepted, enriched profiles.

CSV columns are matched flexibly (case-insensitive; `Name`/`Full Name`, `Company`/`Org`,
`Notes`/`Message`, etc.). Only **Name** is strictly required.

## Tuning

Thresholds and caps live in `.env` (`ACCEPT_THRESHOLD`, `REVIEW_THRESHOLD`,
`MAX_RESULTS_PER_LEAD`, `MAX_CANDIDATES_MATCHED`, `MIN_CONTENT_CHARS`).

## Deploy (Render) — so a teammate can use it

This app needs a **persistent process** (background worker + SQLite), so it runs on
Render/Railway/Fly, **not** Vercel (serverless would time out on the 3–4 min jobs and
lose the SQLite DB).

1. Push this repo to GitHub (see below).
2. Go to https://render.com → **New → Blueprint** → connect this repo. It reads
   `render.yaml`.
3. Render will prompt for the four secret keys (`OPENROUTER_API_KEY`, `TAVILY_API_KEY`,
   `FIRECRAWL_API_KEY`, `APIFY_API_TOKEN`). Paste them there — they are **not** in the repo.
4. Deploy. You get a public URL like `https://leadsearch.onrender.com` to share.

Notes:
- **Free tier** sleeps after ~15 min idle (first request then cold-starts ~30s) and
  uses an ephemeral disk, so the leads DB resets on redeploy. Fine for trying; for
  durable data add a Render Disk (paid) or a hosted Postgres.
- **No login is built in** — anyone with the URL can run leads and spend your API
  credits. Keep the URL private, or ask me to add basic auth before sharing widely.

## Next steps (not built yet)

- Google Sheets input/output (the input layer is already isolated for this).
- B2B enrichment API as a cheap first pass before search-and-scrape.
- Concurrency / batching for large volumes.
