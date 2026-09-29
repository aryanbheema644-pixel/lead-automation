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

## Connect Google Sheets (read source → refine → write destination)

Reads leads from a **source** sheet (e.g. the one Pipedrive feeds) and writes the
refined leads to a **destination** sheet. Uses a Google **service account** — no
browser login, works on the server.

**One-time setup:**
1. https://console.cloud.google.com → create/select a project.
2. **APIs & Services → Library →** enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account.** Create it,
   then under its **Keys** tab → **Add key → JSON** → download the file.
4. Copy the service account's **email** (looks like `name@project.iam.gserviceaccount.com`).
5. **Share your sheets with that email** (the Share button, like sharing with a person):
   - **Source** sheet → **Viewer** (read only)
   - **Destination** sheet → **Editor** (so it can write)
6. Set env vars (locally in `.env`, or in the Render dashboard):
   - `GOOGLE_SERVICE_ACCOUNT_JSON` = the whole JSON file contents (or a path to it)
   - `GOOGLE_SOURCE_SHEET_ID` / `GOOGLE_DEST_SHEET_ID` = the long ID in each sheet's URL:
     `https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit`
   - `GOOGLE_SOURCE_TAB` / `GOOGLE_DEST_TAB` = tab name (optional; blank = first tab)

**Use it:** the dashboard shows **⬇ Pull from Sheet** (imports source rows into the
queue → then Run pipeline) and **⬆ Push to Sheet** (writes processed leads to the
destination sheet; each lead is pushed once). Google is read-*or*-write purely by how
you share each sheet — the API itself supports both.

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
