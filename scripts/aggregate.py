#!/usr/bin/env python3
"""
Aggregate visa-friendly tech job listings from JSearch (RapidAPI) and Adzuna,
classify them for C2C / W-2 / H-1B / OPT / STEM OPT, extract recruiter contact
info where posted, and write data/jobs.json for the static site.

Secrets (set as GitHub Actions repo secrets):
    RAPIDAPI_KEY   - RapidAPI key with access to the JSearch API
    ADZUNA_APP_ID  - Adzuna application id
    ADZUNA_APP_KEY - Adzuna application key

Tunables (environment):
    MAX_RESULTS_PER_QUERY - results kept per query per source (default 25)
    MAX_DAYS_OLD          - drop postings older than this (default 30)
    MAX_TOTAL_JOBS        - cap on jobs.json size (default 600)
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "jobs.json"

# ---------------------------------------------------------------- queries ---
# Broad tech-role queries; the classifier below surfaces the visa-friendly ones.
# Edit freely — each entry costs ~1 API request per source per run.
QUERIES = [
    {"query": "software engineer", "location": "United States"},
    {"query": "java developer", "location": "United States"},
    {"query": "python developer", "location": "United States"},
    {"query": "data engineer", "location": "United States"},
    {"query": "software engineer OPT", "location": "United States"},
    {"query": "developer C2C", "location": "United States"},
]

# ---------------------------------------------------------- classification ---
TAG_PATTERNS = {
    "C2C": [r"\bc2c\b", r"corp[\s\-]?to[\s\-]?corp"],
    "W-2": [r"\bw[\s\-]?2\b"],
    "H-1B": [r"\bh[\s\-]?1b\b", r"visa\s+sponsor", r"sponsor\w*\s+(a\s+)?visa", r"will\s+sponsor"],
    "OPT": [r"\bopt\b"],
    "STEM OPT": [r"stem[\s\-]?opt"],
}
# If a negative matches, the corresponding positive tag is suppressed.
NEGATIVE_PATTERNS = {
    "H-1B": [r"no(t|ne)?\s+(visa\s+)?sponsorship", r"cannot\s+sponsor", r"unable\s+to\s+sponsor",
             r"will\s+not\s+sponsor", r"no\s+sponsorship\s+available"],
    "C2C": [r"no\s+c2c", r"no\s+corp[\s\-]?to[\s\-]?corp"],
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
NOREPLY_RE = re.compile(r"no[\s\-_]?reply|donotreply|do_not_reply", re.I)


def classify(text):
    """Return (tags, negatives) detected in the job text."""
    t = " " + text.lower() + " "
    tags, negatives = [], []
    for tag, patterns in TAG_PATTERNS.items():
        if any(re.search(p, t) for p in patterns):
            tags.append(tag)
    for tag, patterns in NEGATIVE_PATTERNS.items():
        if any(re.search(p, t) for p in patterns):
            negatives.append(tag)
    tags = [x for x in tags if x not in negatives]
    return tags, negatives


def extract_contact(text):
    """Return (email, phone) found in text, or (None, None)."""
    email = next((m for m in EMAIL_RE.findall(text or "") if not NOREPLY_RE.search(m)), None)
    phones = PHONE_RE.findall(text or "")
    phone = phones[0] if phones else None
    return email, phone


def job_id(*parts):
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- sources ----
def fetch_jsearch(api_key, query, location, max_results):
    """JSearch via RapidAPI — aggregates LinkedIn / Indeed / Glassdoor / ZipRecruiter."""
    jobs = []
    try:
        resp = requests.get(
            "https://jsearch.p.rapidapi.com/search",
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
            params={"query": f"{query} in {location}", "page": "1",
                    "num_pages": "1", "date_posted": "month"},
            timeout=30,
        )
        resp.raise_for_status()
        for item in resp.json().get("data", [])[:max_results]:
            jobs.append({
                "title": item.get("job_title") or "",
                "company": item.get("employer_name") or "",
                "location": ", ".join(x for x in [item.get("job_city"), item.get("job_state")]
                                      if x) or item.get("job_country") or "",
                "description": item.get("job_description") or "",
                "apply_url": item.get("job_apply_link") or "",
                "posted_at": item.get("job_posted_at_datetime_utc"),
                "source": item.get("job_publisher") or "JSearch",
                "remote": bool(item.get("job_is_remote")),
                "employment_type": item.get("job_employment_type") or "",
            })
    except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
        print(f"[jsearch] error for '{query}': {e}", file=sys.stderr)
    return jobs


def fetch_adzuna(app_id, app_key, query, location, max_results, max_days_old):
    """Adzuna official API — free tier, requires attribution (see site footer)."""
    jobs = []
    try:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={"app_id": app_id, "app_key": app_key, "results_per_page": max_results,
                    "what": query, "where": location, "max_days_old": max_days_old,
                    "sort_by": "date"},
            timeout=30,
        )
        resp.raise_for_status()
        for item in resp.json().get("results", []):
            company = item.get("company") or {}
            loc = item.get("location") or {}
            jobs.append({
                "title": item.get("title") or "",
                "company": company.get("display_name", "") if isinstance(company, dict) else "",
                "location": loc.get("display_name", "") if isinstance(loc, dict) else "",
                "description": item.get("description") or "",
                "apply_url": item.get("redirect_url") or "",
                "posted_at": item.get("created"),
                "source": "Adzuna",
                "remote": "remote" in (item.get("title") or "").lower(),
                "employment_type": item.get("contract_type") or "",
            })
    except Exception as e:  # noqa: BLE001
        print(f"[adzuna] error for '{query}': {e}", file=sys.stderr)
    return jobs


# ------------------------------------------------------------------- main ----
def main():
    max_results = int(os.getenv("MAX_RESULTS_PER_QUERY", "25"))
    max_days_old = int(os.getenv("MAX_DAYS_OLD", "30"))
    max_total = int(os.getenv("MAX_TOTAL_JOBS", "600"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days_old)

    rapid_key = os.getenv("RAPIDAPI_KEY")
    adzuna_id = os.getenv("ADZUNA_APP_ID")
    adzuna_key = os.getenv("ADZUNA_APP_KEY")

    raw = []
    for q in QUERIES:
        if rapid_key:
            raw += fetch_jsearch(rapid_key, q["query"], q["location"], max_results)
        if adzuna_id and adzuna_key:
            raw += fetch_adzuna(adzuna_id, adzuna_key, q["query"], q["location"],
                                max_results, max_days_old)
    if not rapid_key and not (adzuna_id and adzuna_key):
        print("No API credentials set — keeping existing data.", file=sys.stderr)
        return 0

    seen, jobs = set(), []
    for r in raw:
        if not r["title"] or not r["apply_url"]:
            continue
        jid = job_id(r["title"], r["company"], r["location"])
        if jid in seen:
            continue
        seen.add(jid)
        tags, negatives = classify(f"{r['title']} {r['description']}")
        email, phone = extract_contact(r["description"])
        posted = None
        if r["posted_at"]:
            try:
                posted = datetime.fromisoformat(str(r["posted_at"]).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        if posted and posted < cutoff:
            continue
        jobs.append({
            "id": jid,
            "title": r["title"].strip(),
            "company": r["company"].strip(),
            "location": r["location"].strip(),
            "description": r["description"].strip()[:4000],
            "apply_url": r["apply_url"],
            "posted_at": posted.isoformat() if posted else None,
            "source": r["source"],
            "remote": r["remote"],
            "employment_type": r["employment_type"],
            "tags": tags,
            "sponsorship_notes": [f"no {n}" for n in negatives],
            "recruiter_email": email,
            "recruiter_phone": phone,
        })

    jobs.sort(key=lambda j: j["posted_at"] or "", reverse=True)
    jobs = jobs[:max_total]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(jobs),
            "sources": sorted({j["source"] for j in jobs}),
        },
        "jobs": jobs,
    }, indent=1))
    print(f"Wrote {len(jobs)} jobs to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
