#!/usr/bin/env python3
"""
Daily job search agent.

For each target company, asks Claude (with the server-side web search tool) to
find currently open Data Scientist / Applied Scientist postings and return them
as JSON. The results are then filtered deterministically in Python:

  * title must match the target roles
  * location must be Chicago-area or US remote
  * posted base pay range must go above MIN_SALARY (no-pay postings are kept
    in a separate section so you can check them by hand)

Roles already emailed on earlier days are tracked in seen_jobs.json and flagged
NEW when they first appear. The digest is sent through Gmail SMTP.

Usage:
    python job_agent.py            # search, filter, email
    python job_agent.py --dry-run  # search and filter, print the email instead of sending
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv is optional
    pass

import anthropic

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

COMPANIES = [
    "Apple",
    "Google",
    "Microsoft",
    "AbbVie",
    "BCG (Boston Consulting Group, including BCG X)",
    "Prolaio",
    "Netflix",
]

ROLE_TITLES = ["Data Scientist", "Applied Scientist"]
SKILLS = ["Python", "pandas", "SQL", "C++", "git"]
MIN_SALARY = 130_000

# Titles containing any of these are dropped even if they mention a target role.
EXCLUDE_TITLE_WORDS = ["intern", "internship", "manager", "director", "head of", "recruiter"]

CHICAGO_WORDS = ["chicago", "evanston", "oak brook", "schaumburg", "naperville",
                 "deerfield", "north chicago", "lake forest", "mettawa", "illinois, us", ", il"]
REMOTE_WORDS = ["remote", "home office", "work from home", "anywhere in the us", "virtual"]

MODEL = os.environ.get("JOB_AGENT_MODEL", "claude-sonnet-5-5")
MAX_SEARCHES_PER_COMPANY = int(os.environ.get("JOB_AGENT_MAX_SEARCHES", "8"))

EMAIL_TO = os.environ.get("EMAIL_TO", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

STATE_FILE = Path(__file__).with_name("seen_jobs.json")
LOG_DIR = Path(__file__).with_name("runs")

# --------------------------------------------------------------------------- #
# Step 1: search with Claude
# --------------------------------------------------------------------------- #

PROMPT_TEMPLATE = """Find job postings at {company} that are OPEN right now (today is {today}).

Target titles: {titles} (including seniority variants such as Senior, Staff, Principal,
and close variants such as "Applied Data Scientist" or "Data Scientist, Machine Learning").
Target locations: Chicago, IL metro area, OR remote within the United States.

Search the company's own careers site first, then job boards (LinkedIn, Greenhouse,
Lever, Workday, Built In Chicago) if needed. Open postings to confirm location and pay
when you can. Look for these skills in each posting: {skills}.

Rules:
- Only include postings you actually found in search results. Never invent a posting,
  a URL, or a salary.
- Report the base pay range exactly as posted, as integers in US dollars per year.
  If the posting shows no pay, use null.
- It is fine to include borderline postings; filtering happens later.
- If you found nothing, return an empty list.

Reply with ONLY a JSON array, no prose, no code fences. Each element:
{{"title": str, "company": "{company_short}", "location": str, "remote": bool,
  "salary_min": int|null, "salary_max": int|null, "url": str,
  "posted_date": str|null, "skills_mentioned": [str], "job_id": str|null}}
"""


def search_company(client: anthropic.Anthropic, company: str) -> list[dict]:
    company_short = company.split(" (")[0]
    prompt = PROMPT_TEMPLATE.format(
        company=company,
        company_short=company_short,
        today=dt.date.today().isoformat(),
        titles=" or ".join(f'"{t}"' for t in ROLE_TITLES),
        skills=", ".join(SKILLS),
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": MAX_SEARCHES_PER_COMPANY}]

    # Server-side tool loops can pause; resend to let Claude continue.
    for _ in range(5):
        resp = client.messages.create(model=MODEL, max_tokens=8000,
                                      tools=tools, messages=messages)
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        break

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    jobs = parse_json_list(text)
    for j in jobs:
        j.setdefault("company", company_short)
    return jobs


def parse_json_list(text: str) -> list[dict]:
    """Pull the JSON array out of the model's reply, tolerating stray text."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("title") and d.get("url")]

# --------------------------------------------------------------------------- #
# Step 2: filter in plain Python
# --------------------------------------------------------------------------- #


def title_ok(title: str) -> bool:
    t = title.lower()
    if any(w in t for w in EXCLUDE_TITLE_WORDS):
        return False
    return any(r.lower() in t for r in ROLE_TITLES)


def location_tag(job: dict) -> str | None:
    loc = (job.get("location") or "").lower()
    if any(w in loc for w in CHICAGO_WORDS):
        return "Chicago"
    if job.get("remote") or any(w in loc for w in REMOTE_WORDS):
        # Reject remote roles explicitly outside the US.
        if any(c in loc for c in ["uk", "canada", "india", "europe", "emea", "apac", "germany"]):
            return None
        return "Remote"
    return None


def to_int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "").replace("$", "")))
    except (TypeError, ValueError):
        return None


def salary_status(job: dict) -> str:
    """'ok', 'low', or 'unknown'."""
    hi = to_int(job.get("salary_max")) or to_int(job.get("salary_min"))
    if hi is None:
        return "unknown"
    return "ok" if hi > MIN_SALARY else "low"


def skill_matches(job: dict) -> list[str]:
    mentioned = {s.lower() for s in job.get("skills_mentioned") or []}
    return [s for s in SKILLS if s.lower() in mentioned]


def job_key(job: dict) -> str:
    if job.get("job_id"):
        return f'{job["company"]}::{job["job_id"]}'
    url = re.sub(r"[?#].*$", "", job["url"]).rstrip("/").lower()
    return url


def filter_jobs(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (matches, pay_not_listed), de-duplicated."""
    matches, unknown, seen = [], [], set()
    for j in raw:
        key = job_key(j)
        if key in seen or not title_ok(j["title"]):
            continue
        tag = location_tag(j)
        if tag is None:
            continue
        status = salary_status(j)
        if status == "low":
            continue
        seen.add(key)
        j["_key"], j["_where"], j["_skills"] = key, tag, skill_matches(j)
        (matches if status == "ok" else unknown).append(j)
    sort = lambda js: sorted(js, key=lambda x: (-len(x["_skills"]), x["company"], x["title"]))
    return sort(matches), sort(unknown)

# --------------------------------------------------------------------------- #
# Step 3: remember what was already sent
# --------------------------------------------------------------------------- #


def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def mark_new(jobs: list[dict], seen: dict) -> None:
    today = dt.date.today().isoformat()
    for j in jobs:
        j["_new"] = j["_key"] not in seen
        seen.setdefault(j["_key"], today)

# --------------------------------------------------------------------------- #
# Step 4: build and send the email
# --------------------------------------------------------------------------- #


def fmt_pay(j: dict) -> str:
    lo, hi = to_int(j.get("salary_min")), to_int(j.get("salary_max"))
    if lo and hi:
        return f"${lo:,} to ${hi:,}"
    if hi or lo:
        return f"${(hi or lo):,}"
    return "not listed"


def render_rows(jobs: list[dict]) -> str:
    rows = []
    for j in jobs:
        new = '<b style="color:#c0392b">NEW</b> ' if j.get("_new") else ""
        skills = ", ".join(j["_skills"]) or "none listed"
        posted = f' · posted {escape(j["posted_date"])}' if j.get("posted_date") else ""
        rows.append(
            f'<li>{new}<a href="{escape(j["url"])}">{escape(j["title"])}</a>'
            f' · <b>{escape(j["company"])}</b> · {j["_where"]} ({escape(j.get("location") or "")})'
            f'<br>Pay: {fmt_pay(j)} · Skills: {skills}{posted}</li>'
        )
    return "<ul>" + "\n".join(rows) + "</ul>" if rows else "<p>None today.</p>"


def build_email(matches, unknown, failed) -> tuple[str, str]:
    today = dt.date.today().strftime("%b %d, %Y")
    n_new = sum(1 for j in matches + unknown if j.get("_new"))
    subject = f"Daily job scan: {len(matches)} matching roles, {n_new} new ({today})"
    body = (
        f"<h2>Roles paying over ${MIN_SALARY:,} (Chicago or remote)</h2>{render_rows(matches)}"
        f"<h3>Pay not listed (check manually)</h3>{render_rows(unknown)}"
    )
    if failed:
        body += f"<p><i>Could not check today: {escape(', '.join(failed))}</i></p>"
    body += ("<p style='color:#888;font-size:12px'>Results come from web search and can miss "
             "postings or be out of date; always confirm on the company site.</p>")
    return subject, body


def send_email(subject: str, html: str) -> None:
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see README).")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_ADDRESS, EMAIL_TO or GMAIL_ADDRESS
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.send_message(msg)

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the email instead of sending")
    args = ap.parse_args()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    raw, failed = [], []
    for company in COMPANIES:
        name = company.split(" (")[0]
        try:
            found = search_company(client, company)
            print(f"{name}: {len(found)} candidate postings", file=sys.stderr)
            raw.extend(found)
        except Exception as e:  # keep going if one company fails
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            failed.append(name)

    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"{dt.date.today().isoformat()}_raw.json").write_text(json.dumps(raw, indent=2))

    matches, unknown = filter_jobs(raw)
    seen = load_seen()
    mark_new(matches + unknown, seen)
    subject, html = build_email(matches, unknown, failed)

    if args.dry_run:
        print(subject)
        print(re.sub(r"<[^>]+>", "", html.replace("<li>", "\n- ").replace("<br>", "\n  ")))
        return

    send_email(subject, html)
    STATE_FILE.write_text(json.dumps(seen, indent=2))  # only remember after a successful send
    print(f"Sent: {subject}", file=sys.stderr)


if __name__ == "__main__":
    main()
