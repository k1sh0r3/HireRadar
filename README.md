# HireRadar

A job board for visa-friendly tech positions — **C2C**, **W-2**, **H-1B sponsorship**,
**OPT** and **STEM OPT** — refreshed automatically once a day and hosted free on
GitHub Pages.

## How it works

```
GitHub Actions (cron, daily)
        │
        ▼
scripts/aggregate.py ──┬─► JSearch API (LinkedIn / Indeed / Glassdoor / ZipRecruiter)
                       ├──► Adzuna API (free tier, needs app id + key)
                       └─► Remotive API (free, keyless, remote jobs)
        │
        │   classify: C2C / W-2 / H-1B / OPT / STEM OPT keywords
        │   extract: recruiter email + phone from the posting text
        │   merge with previous listings, dedupe, prune > 30 days old
        ▼
data/jobs.json ──► static site (index.html + assets/) filters it in your browser
```

GitHub Pages serves only static files, so the scheduled workflow does the fetching
and commits the results — no server needed.

## Setup (one time)

1. **JSearch key.** Sign up free at [RapidAPI](https://rapidapi.com/),
   subscribe to the **JSearch** API (free tier ≈ 200–300 requests/month), and copy your key.
2. In this repo go to **Settings → Secrets and variables → Actions → New repository secret**
   and add:
   - `RAPIDAPI_KEY` (required — JSearch)
   - `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` (optional second source — free
     [Adzuna API](https://developer.adzuna.com/) credentials; Remotive needs no key)
3. **Enable Pages:** Settings → Pages → *Deploy from a branch* → branch `main`, folder `/ (root)`.
4. Trigger the first refresh manually: **Actions → Refresh job listings → Run workflow**.

Without keys the corresponding source is simply skipped; if **every** source fails
(bad keys, outage, …), the workflow keeps the existing `data/jobs.json` untouched,
so the site always shows the last good data.

## Quota math

Default config runs 9 JSearch queries + 4 Remotive searches once daily
≈ 270 JSearch requests/month — near the top of JSearch's free tier
(≈ 200–300), so keep the query list short. Remotive is free and keyless
(its guidance asks for only a few requests per day).
Each refresh *accumulates*: listings persist across runs and are pruned
only when older than 30 days (`MAX_DAYS_OLD`) or beyond the 600-listing
cap (`MAX_TOTAL_JOBS`). Tunables via env vars: `MAX_RESULTS_PER_QUERY` (25),
`MAX_DAYS_OLD` (30), `MAX_TOTAL_JOBS` (600). Adzuna runs the same 9 queries once
daily (9 × 30 ≈ 270 requests/month — inside its free tier of 2,500/month).

## Run the aggregator locally

```bash
pip install -r requirements.txt
RAPIDAPI_KEY=... python scripts/aggregate.py
```

## Notes

- Recruiter email/phone is shown **only when the posting itself includes it** —
  many postings don't.
- Tags are keyword-based (e.g. "C2C", "H-1B", "sponsorship"); always verify on the
  original posting before applying. Postings that explicitly rule things out
  ("no sponsorship", "no C2C") are flagged, not tagged.
- "Search on Dice / Monster" buttons deep-link your current query into those sites,
  which offer no public API.
