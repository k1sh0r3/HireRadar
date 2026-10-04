#!/usr/bin/env python3
"""
Aggregate visa-friendly tech job listings from JSearch (RapidAPI), Adzuna,
keyless startup ATS boards (Ashby / Greenhouse / Lever), and HN "Who is hiring?",
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
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "jobs.json"

JSEARCH_HOST = "jsearch.p.rapidapi.com"
# JSearch retired /search (now 404s) in favor of /search-v2; subscriptions
# created before the migration may only expose /search, so we probe both.
_JSEARCH_PATHS = ("/search-v2", "/search")
_JSEARCH_PATH = None  # working path, resolved once per run and cached

# ---------------------------------------------------------------- queries ---
# AI/ML-tuned queries; the classifier below surfaces the visa-friendly ones.
# Edit freely — each entry costs ~1 API request per source per run.
QUERIES = [
    {"query": "machine learning engineer", "location": "United States"},
    {"query": "AI engineer", "location": "United States"},
    {"query": "LLM engineer", "location": "United States"},
    {"query": "MLOps engineer", "location": "United States"},
    {"query": "AI software engineer", "location": "United States"},
    {"query": "data engineer", "location": "United States"},
    {"query": "data scientist", "location": "United States"},
    {"query": "software engineer OPT", "location": "United States"},
    {"query": "software engineer", "location": "United States"},
]

# Remotive (free, keyless): remote-only jobs. Their API guidance asks for
# only a few requests per day, so keep this list short.
REMOTIVE_QUERIES = ["machine learning engineer", "AI engineer", "data engineer", "software engineer"]

# ------------------------------------------------------- startup boards -----
# Keyless public ATS APIs. Startup-heavy and AI-dense — exactly where AI
# software-engineering roles concentrate. Slugs verified live 2026-10-04;
# dead slugs are skipped silently so these lists can be extended freely.
ASHBY_BOARDS = {
    "openai": "OpenAI", "perplexity": "Perplexity", "elevenlabs": "ElevenLabs",
    "cohere": "Cohere", "cursor": "Cursor", "sierra": "Sierra",
    "writer": "Writer", "modal": "Modal", "langchain": "LangChain",
    "deepgram": "Deepgram", "hebbia-ai": "Hebbia", "eliseai": "EliseAI",
    "gptzero": "GPTZero", "bedrock-robotics": "Bedrock Robotics",
    "linear": "Linear", "ramp": "Ramp", "gigaml": "GigaML", "decagon": "Decagon",
}
GREENHOUSE_BOARDS = {
    "anthropic": "Anthropic", "databricks": "Databricks", "scaleai": "Scale AI",
    "assemblyai": "AssemblyAI", "gleanwork": "Glean", "togetherai": "Together AI",
    "xai": "xAI", "stabilityai": "Stability AI", "snorkelai": "Snorkel AI",
    "dataiku": "Dataiku", "figureai": "Figure AI", "inflectionai": "Inflection AI",
    "labelbox": "Labelbox", "arizeai": "Arize AI", "vectara": "Vectara",
    "sambanovasystems": "SambaNova", "inceptive": "Inceptive", "cresta": "Cresta",
}
LEVER_BOARDS = {
    "palantir": "Palantir", "shieldai": "Shield AI",
    "field-ai": "Field AI", "epoch-ai": "Epoch AI",
}

# Boards list every opening (sales, marketing, …) — keep tech roles only so
# the board stays a tech board and the job cap isn't eaten by noise.
TECH_TITLE_RE = re.compile(
    r"engineer|developer|data scien|machine learning|researcher|scientist|"
    r"research scien|devops|\bsre\b|software|architect|data analyst|"
    r"\bqa\b|systems|infrastructure|platform|technical",
    re.IGNORECASE,
)

# AI/ML relevance — flags roles worth a closer look for AI job seekers.
AI_ML_RE = re.compile(
    r"machine learning|deep learning|\bllm\b|large language|artificial intelligence|"
    r"\bml\b|\bai\b|data scien|mlops|generative ai|\bgenai\b|prompt engineer|"
    r"\brag\b|fine[\s\-]?tun|diffusion|transformer|reinforcement learning|"
    r"\bnlp\b|computer vision",
    re.IGNORECASE,
)

# Exact target titles for an AI Software Engineer hunt — the strongest signal.
STRONG_TITLE_KEYWORDS = (
    "ai engineer", "machine learning", "ml engineer", "llm",
    "applied ai", "genai", "ai software", "ai researcher",
    "ai scientist", "ai developer", "prompt engineer",
)


def is_strong_match(title):
    """True when the title is squarely an AI engineering role."""
    t = (title or "").lower()
    if any(k in t for k in STRONG_TITLE_KEYWORDS):
        return True
    return bool(re.search(r"\bai\b", t) and "engineer" in t)


# Salary extraction — "$180k – $220k", "$75,000 - $85,000 per year", "up to $200k".
SALARY_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*(?:–|—|-|\bto\b)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?"
)
SALARY_SINGLE_RE = re.compile(
    r"(?:up to|from|starting at|base salary:?|salary:?|pay:?)\s*\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?"
    r"|\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*(?:per hour|/hr\b|hourly)",
    re.IGNORECASE,
)
HOURLY_RE = re.compile(r"per hour|/hr\b|hourly", re.IGNORECASE)


def _sal_num(s, k):
    try:
        v = float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    return int(v * 1000) if k else int(v)


def extract_salary(text):
    """Return (min_annual_usd, max_annual_usd, display_text) or (None, None, None)."""
    if not text:
        return None, None, None
    t = text[:6000]
    m = SALARY_RANGE_RE.search(t)
    lo = hi = None
    if m:
        lo, hi = _sal_num(m.group(1), m.group(2)), _sal_num(m.group(3), m.group(4))
    else:
        m = SALARY_SINGLE_RE.search(t)
        if m:
            num, k = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            lo = hi = _sal_num(num, k)
    if lo is None:
        return None, None, None
    window = t[max(0, m.start() - 40):m.end() + 40]
    if HOURLY_RE.search(window):  # hourly rate -> annualize
        lo = int(lo * 2080) if lo else None
        hi = int(hi * 2080) if hi else None

    def fmt(v):
        if not v:
            return ""
        return f"${v / 1000:g}k" if v % 1000 == 0 else f"${v:,}"
    if lo and hi and lo != hi:
        disp = f"{fmt(lo)} – {fmt(hi)}"
    else:
        disp = fmt(lo or hi)
    return lo, hi, disp


# Fuzzy dedup — "Sr. ML Engineer" and "Senior Machine Learning Engineer" at the
# same company/location are the same listing.
_TITLE_NORM_RES = [
    (re.compile(r"\bsr\.?\b"), "senior"),
    (re.compile(r"\bjr\.?\b"), "junior"),
    (re.compile(r"\bml\b"), "machine learning"),
    (re.compile(r"\bswe\b"), "software engineer"),
    (re.compile(r"[^a-z0-9 ]"), " "),
    (re.compile(r"\s+"), " "),
]


def norm_key(title, company, location):
    t = (title or "").lower()
    for rx, rep in _TITLE_NORM_RES:
        t = rx.sub(rep, t)
    return job_id(t.strip(), company, location)


# Role words used to tell "Role | Company" apart from "Company — Role".
ROLE_CHUNK_RE = re.compile(
    r"engineer|developer|designer|scientist|researcher|manager|analyst|architect|intern",
    re.IGNORECASE,
)
# HN "Who is hiring?" — monthly thread, startup-dense. Comments are freeform;
# keep the ones that look AI/ML/data/engineering relevant.
HN_KEEP_RE = re.compile(
    r"\bai\b|machine learning|deep learning|\bllm\b|large language|"
    r"\bml\b|artificial intelligence|data scien|mlops|generative ai|"
    r"\bgenai\b|engineer|developer|software|research scien",
    re.IGNORECASE,
)

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
def _jsearch_params(path, query, location):
    q = f"{query} in {location}"
    if path == "/search-v2":
        return {"query": q, "date_posted": "month"}  # v2 is cursor-based: no page/num_pages
    return {"query": q, "page": "1", "num_pages": "1", "date_posted": "month"}


def _jsearch_rows(payload):
    """Unwrap the job list from v1 ({data: [...]}) or v2 ({data: {jobs: [...]}}) shapes."""
    data = payload.get("data", payload) if isinstance(payload, dict) else []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("jobs", "results", "data"):
            nested = data.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _normalize_jsearch_item(item):
    """Map v1 fields, falling back to v2-only variants (job_location, apply_options, …)."""
    apply_link = item.get("job_apply_link") or ""
    if not apply_link:
        for opt in item.get("apply_options") or []:
            if isinstance(opt, dict):
                apply_link = opt.get("apply_link") or opt.get("link") or ""
                if apply_link:
                    break
    posted = item.get("job_posted_at_datetime_utc")
    ts = item.get("job_posted_at_timestamp")
    if not posted and isinstance(ts, (int, float)):
        posted = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    remote = item.get("job_is_remote")
    if not isinstance(remote, bool):
        arrangement = str(item.get("job_work_arrangement") or item.get("work_arrangement") or "")
        remote = bool(re.search(r"remote|work\s*from\s*home", arrangement, re.IGNORECASE))
    emp_types = item.get("job_employment_types") or []
    return {
        "title": item.get("job_title") or "",
        "company": item.get("employer_name") or "",
        "location": ", ".join(x for x in [item.get("job_city"), item.get("job_state")] if x)
                    or item.get("job_country") or item.get("job_location") or "",
        "description": item.get("job_description") or "",
        "apply_url": apply_link,
        "posted_at": posted,
        "source": item.get("job_publisher") or "JSearch",
        "remote": bool(remote),
        "employment_type": item.get("job_employment_type")
                           or (emp_types[0] if emp_types else "") or "",
    }


def fetch_jsearch(api_key, query, location, max_results):
    """JSearch via RapidAPI — aggregates LinkedIn / Indeed / Glassdoor / ZipRecruiter.

    Tries /search-v2 first (the old /search path was retired and now 404s),
    falling back to /search for subscriptions that predate the migration.
    The working path is cached for the rest of the run.

    Returns (jobs, ok): ok=False means the API call itself failed, so the caller
    can tell "no results" apart from "source is down".
    """
    global _JSEARCH_PATH
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    paths = [_JSEARCH_PATH] if _JSEARCH_PATH else list(_JSEARCH_PATHS)
    for path in paths:
        try:
            resp = requests.get(f"https://{JSEARCH_HOST}{path}", headers=headers,
                                params=_jsearch_params(path, query, location), timeout=30)
        except Exception as e:  # noqa: BLE001 — network error; other path won't help
            print(f"[jsearch] error for '{query}': {e}", file=sys.stderr)
            return [], False
        if resp.status_code == 404 and "does not exist" in resp.text.lower():
            continue  # endpoint not exposed on this subscription — try the other path
        if resp.status_code != 200:
            print(f"[jsearch] error for '{query}': HTTP {resp.status_code}: "
                  f"{resp.text[:200]}", file=sys.stderr)
            return [], False
        try:
            rows = _jsearch_rows(resp.json())
        except Exception as e:  # noqa: BLE001 — 200 with an unparsable body
            print(f"[jsearch] error for '{query}': bad response body: {e}", file=sys.stderr)
            return [], False
        _JSEARCH_PATH = path
        return [_normalize_jsearch_item(it) for it in rows[:max_results]], True
    print("[jsearch] error: subscription exposes neither /search-v2 nor /search",
          file=sys.stderr)
    return [], False


def fetch_adzuna(app_id, app_key, query, location, max_results, max_days_old):
    """Adzuna official API — free tier, requires attribution (see site footer).

    Returns (jobs, ok): ok=False means the API call itself failed.
    """
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
        return jobs, False
    return jobs, True


def fetch_remotive(query, max_results):
    """Remotive API — free, keyless, remote-only jobs. Returns (jobs, ok).

    Terms require linking back to the Remotive listing (we use their URL as
    the apply link) and crediting Remotive as a source (footer of the site).
    """
    jobs = []
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": query, "limit": max_results},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[remotive] error for '{query}': HTTP {resp.status_code}", file=sys.stderr)
            return [], False
        for item in resp.json().get("jobs", [])[:max_results]:
            jobs.append({
                "title": item.get("title") or "",
                "company": item.get("company_name") or "",
                "location": item.get("candidate_required_location") or "Remote",
                "description": item.get("description") or "",
                "apply_url": item.get("url") or "",
                "posted_at": item.get("publication_date"),
                "source": "Remotive",
                "remote": True,
                "employment_type": (item.get("job_type") or "").replace("_", " ").title(),
            })
    except Exception as e:  # noqa: BLE001
        print(f"[remotive] error for '{query}': {e}", file=sys.stderr)
        return [], False
    return jobs, True


def _strip_html(html_text):
    """Plain text out of an HTML blob for descriptions."""
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _board_get(url, label):
    """GET a keyless board endpoint. Returns (parsed_json, ok)."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "HireRadar/1.0"})
        resp.raise_for_status()
        return resp.json(), True
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] board fetch error: {e}", file=sys.stderr)
        return None, False


def fetch_ashby(slug, company, max_results):
    """Ashby public posting API — keyless. Returns (jobs, ok)."""
    data, ok = _board_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "ashby")
    if not ok:
        return [], False
    jobs = []
    for j in data.get("jobs") or []:
        title = j.get("title") or ""
        if not TECH_TITLE_RE.search(title):
            continue
        jobs.append({
            "title": title,
            "company": company,
            "location": j.get("location") or "",
            "description": j.get("descriptionPlain") or "",
            "apply_url": j.get("jobUrl") or "",
            "posted_at": j.get("publishedAt"),
            "source": "Ashby",
            "remote": bool(j.get("isRemote")),
            "employment_type": (j.get("employmentType") or "").replace("FullTime", "Full-time"),
        })
        if len(jobs) >= max_results:
            break
    return jobs, True


def fetch_greenhouse(token, company, max_results):
    """Greenhouse public boards API — keyless. Returns (jobs, ok)."""
    data, ok = _board_get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", "greenhouse")
    if not ok:
        return [], False
    jobs = []
    for j in data.get("jobs") or []:
        title = j.get("title") or ""
        if not TECH_TITLE_RE.search(title):
            continue
        loc = j.get("location") or {}
        jobs.append({
            "title": title,
            "company": company,
            "location": loc.get("name", "") if isinstance(loc, dict) else "",
            "description": _strip_html(j.get("content") or ""),
            "apply_url": j.get("absolute_url") or "",
            "posted_at": j.get("updated_at"),
            "source": "Greenhouse",
            "remote": "remote" in (loc.get("name", "") if isinstance(loc, dict) else "").lower(),
            "employment_type": "",
        })
        if len(jobs) >= max_results:
            break
    return jobs, True


def fetch_lever(site, company, max_results):
    """Lever public postings API — keyless. Returns (jobs, ok)."""
    data, ok = _board_get(f"https://api.lever.co/v0/postings/{site}?mode=json", "lever")
    if not ok:
        return [], False
    jobs = []
    for j in data or []:
        title = j.get("text") or ""
        if not TECH_TITLE_RE.search(title):
            continue
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        posted = None
        if isinstance(created, (int, float)):
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        jobs.append({
            "title": title,
            "company": company,
            "location": cats.get("location") or "",
            "description": j.get("descriptionPlain") or "",
            "apply_url": j.get("hostedUrl") or "",
            "posted_at": posted,
            "source": "Lever",
            "remote": "remote" in str(j.get("workplaceType") or "").lower(),
            "employment_type": cats.get("commitment") or "",
        })
        if len(jobs) >= max_results:
            break
    return jobs, True


def fetch_hn_hiring(max_results):
    """Current month's 'Ask HN: Who is hiring?' thread via the Algolia API — keyless.

    Keeps top-level comments that look AI/ML/engineering relevant.
    Returns (jobs, ok).
    """
    try:
        now = datetime.now(timezone.utc)
        # Threads post on the 1st; early in the month the current one may not
        # exist yet, so fall back to last month's.
        month_queries = [now.strftime("%B %Y"),
                         (now.replace(day=1) - timedelta(days=1)).strftime("%B %Y")]
        thread_id, thread_month = None, ""
        for mq in month_queries:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": f"Ask HN: Who is hiring? ({mq})",
                        "tags": "story", "hitsPerPage": 10},
                timeout=30, headers={"User-Agent": "HireRadar/1.0"})
            resp.raise_for_status()
            hits = [h for h in resp.json().get("hits", [])
                    if h.get("author") == "whoishiring" and "hiring?" in (h.get("title") or "")]
            if hits:
                best = max(hits, key=lambda h: h.get("created_at", ""))
                thread_id, thread_month = best.get("objectID"), mq
                break
        if not thread_id:
            return [], True
        resp = requests.get(f"https://hn.algolia.com/api/v1/items/{thread_id}",
                            timeout=30, headers={"User-Agent": "HireRadar/1.0"})
        resp.raise_for_status()
        children = resp.json().get("children") or []
    except Exception as e:  # noqa: BLE001
        print(f"[hn] fetch error: {e}", file=sys.stderr)
        return [], False
    jobs = []
    for c in children:
        text = _strip_html(c.get("text") or "")
        if not HN_KEEP_RE.search(text):
            continue
        first = text.split("\n")[0].strip()
        # Split on em/en dashes, pipes, or space-separated hyphens — but NOT
        # the hyphens inside words like "Full-Stack".
        chunks = [x.strip() for x in re.split(r"\s*[—–|]\s*|\s+-\s+", first) if x.strip()]
        title, company, location = "", "HN Hiring", ""
        if chunks:
            role_idx = next((i for i, c in enumerate(chunks)
                             if ROLE_CHUNK_RE.search(c)), None)
            if role_idx is None:
                # No recognizable role chunk — keep the opener as the title.
                title = first[:120]
            else:
                title = chunks[role_idx][:120]
                rest = [c for i, c in enumerate(chunks)
                        if i != role_idx and not ROLE_CHUNK_RE.search(c)
                        and len(c) <= 80]
                if rest:
                    company = re.sub(r"\s*\(.*?\)\s*", "", rest[0]).strip()[:80] or "HN Hiring"
                    m = re.search(r"\((.*?)\)", rest[0])
                    location = m.group(1).strip() if m else ""
        if not title:
            continue
        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "description": text,
            "apply_url": f"https://news.ycombinator.com/item?id={c.get('id')}",
            "posted_at": c.get("created_at"),
            "source": "HN Hiring",
            "remote": "remote" in text.lower(),
            "employment_type": "",
        })
        if len(jobs) >= max_results:
            break
    return jobs, True


def _too_old(posted_at, cutoff):
    """True when a listing is older than the retention cutoff."""
    if not posted_at:
        return False
    try:
        d = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d < cutoff
    except ValueError:
        return False


# ------------------------------------------------------------------- main ----
def main():
    max_results = int(os.getenv("MAX_RESULTS_PER_QUERY", "25"))
    max_days_old = int(os.getenv("MAX_DAYS_OLD", "30"))
    max_total = int(os.getenv("MAX_TOTAL_JOBS", "600"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days_old)

    rapid_key = os.getenv("RAPIDAPI_KEY")
    adzuna_id = os.getenv("ADZUNA_APP_ID")
    adzuna_key = os.getenv("ADZUNA_APP_KEY")

    raw, any_ok = [], False
    for q in QUERIES:
        if rapid_key:
            fetched, ok = fetch_jsearch(rapid_key, q["query"], q["location"], max_results)
            raw += fetched
            any_ok = any_ok or ok
        if adzuna_id and adzuna_key:
            fetched, ok = fetch_adzuna(adzuna_id, adzuna_key, q["query"], q["location"],
                                       max_results, max_days_old)
            raw += fetched
            any_ok = any_ok or ok
    for rq in REMOTIVE_QUERIES:  # free, keyless
        fetched, ok = fetch_remotive(rq, max_results)
        raw += fetched
        any_ok = any_ok or ok
    # Keyless startup boards (Ashby / Greenhouse / Lever) + HN hiring thread.
    for slug, company in ASHBY_BOARDS.items():
        fetched, ok = fetch_ashby(slug, company, 80)
        raw += fetched
        any_ok = any_ok or ok
    for token, company in GREENHOUSE_BOARDS.items():
        fetched, ok = fetch_greenhouse(token, company, 80)
        raw += fetched
        any_ok = any_ok or ok
    for site, company in LEVER_BOARDS.items():
        fetched, ok = fetch_lever(site, company, 80)
        raw += fetched
        any_ok = any_ok or ok
    fetched, ok = fetch_hn_hiring(40)
    raw += fetched
    any_ok = any_ok or ok

    seen, norm_seen, jobs = set(), set(), []
    for r in raw:
        if not r["title"] or not r["apply_url"]:
            continue
        jid = job_id(r["title"], r["company"], r["location"])
        nkid = norm_key(r["title"], r["company"], r["location"])
        if jid in seen or nkid in norm_seen:
            continue
        seen.add(jid)
        norm_seen.add(nkid)
        tags, negatives = classify(f"{r['title']} {r['description']}")
        email, phone = extract_contact(r["description"])
        salary_min, salary_max, salary_text = extract_salary(r["description"])
        posted = None
        if r["posted_at"]:
            try:
                posted = datetime.fromisoformat(str(r["posted_at"]).replace("Z", "+00:00"))
                if posted.tzinfo is None:  # e.g. Remotive's naive timestamps -> UTC
                    posted = posted.replace(tzinfo=timezone.utc)
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
            "ai_ml": bool(AI_ML_RE.search(f"{r['title']} {r['description'][:1500]}")),
            "strong_match": is_strong_match(r["title"]),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_text": salary_text,
        })

    # Merge with previous listings so each refresh accumulates instead of
    # replacing. Listings persist across runs; only ones older than the
    # retention window (or past the cap) are pruned.
    now_iso = datetime.now(timezone.utc).isoformat()
    previous = {}
    try:
        old_data = json.loads(OUT_PATH.read_text())
        for j in old_data.get("jobs", []):
            if j.get("id"):
                previous[j["id"]] = j
    except Exception:  # noqa: BLE001
        pass
    # Fuzzy map: normalized key -> stored id, so re-fetched near-dupes merge
    # into the existing listing instead of duplicating it.
    norm_to_id = {norm_key(j.get("title"), j.get("company"), j.get("location")): j["id"]
                  for j in previous.values() if j.get("id")}
    for j in jobs:
        prev = previous.get(j["id"])
        if prev is None:
            pid = norm_to_id.get(norm_key(j["title"], j["company"], j["location"]))
            prev = previous.get(pid) if pid else None
            if prev is not None:
                j["id"] = prev["id"]  # keep the original id and its history
        j["first_seen_at"] = prev.get("first_seen_at") if prev else now_iso
        previous[j["id"]] = j  # refresh all fields for re-fetched jobs
        norm_to_id[norm_key(j["title"], j["company"], j["location"])] = j["id"]
    jobs = [j for j in previous.values() if not _too_old(j.get("posted_at"), cutoff)]
    jobs.sort(key=lambda j: j["posted_at"] or j.get("first_seen_at") or "", reverse=True)
    jobs = jobs[:max_total]

    if not jobs and not any_ok:
        # Every source failed (bad key, outage, …) — keep the last good data
        # instead of blanking the site.
        print("All sources failed — keeping existing data.", file=sys.stderr)
        return 1

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
