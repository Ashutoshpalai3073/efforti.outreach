"""Apollo API lead pull — auto-fetch C-suite contacts by ICP filters.

Replaces manual CSV export: hit Apollo's People Search with your ICP filters,
optionally reveal emails via the enrichment endpoint, then run every contact
through the SAME gates as the CSV importer (syntax, suppression, dedupe,
one-lead-per-domain).

NOTE: Apollo's response shape and whether emails come back unlocked depend on
your plan. Run a 1-page pull with your key first (via /leads/apollo_pull) and
check the Activity log before pulling at volume. Requires APOLLO_API_KEY.

Docs: https://docs.apollo.io/reference/people-search
"""
import os
import time

import requests

from .importer import normalize_domain, verify_email
from .models import Lead, Suppression, log

# People Search (api_search) returns OBFUSCATED previews — no email, last name
# hidden — plus a per-record has_email flag. Real name/email/company come only
# from the enrichment call (people/match), which is what reveals + costs a credit.
SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"

# Default Efforti ICP: CEO / founder / chief-of-staff at 30-200 person companies.
DEFAULT_TITLES = ["CEO", "Founder", "Co-Founder", "Chief Executive Officer",
                  "Chief of Staff"]
DEFAULT_SIZE_RANGES = ["21,50", "51,100", "101,200"]

LOCKED_EMAIL_MARKERS = ("not_unlocked", "email_not_unlocked", "domain.com")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", ""),
    }


def _search_page(titles, size_ranges, keywords, locations, page, per_page):
    body = {
        "person_titles": titles,
        "organization_num_employees_ranges": size_ranges,
        "page": page,
        "per_page": per_page,
    }
    if keywords:
        body["q_organization_keyword_tags"] = keywords
    if locations:
        body["person_locations"] = locations
    r = requests.post(SEARCH_URL, headers=_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _enrich_person(person_id: str, retries: int = 2) -> dict:
    """Enrichment call: reveals real name, email, and full org data for one
    person (consumes an Apollo credit). Retries on rate-limit (429) with backoff
    so a burst of reveals doesn't silently drop leads. Returns the person obj."""
    for attempt in range(retries + 1):
        try:
            r = requests.post(MATCH_URL, headers=_headers(),
                              json={"id": person_id,
                                    "reveal_personal_emails": False}, timeout=30)
            if r.status_code == 429:               # rate limited — wait and retry
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return (r.json().get("person") or {})
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return {}
    return {}


def _usable_email(email: str) -> bool:
    return bool(email) and not any(m in email.lower() for m in LOCKED_EMAIL_MARKERS)


def _person_to_fields(p: dict) -> dict:
    """Map an ENRICHED person object (from people/match) to our Lead fields."""
    org = p.get("organization") or {}
    return {
        "first_name": p.get("first_name", "") or "",
        "last_name": p.get("last_name", "") or "",
        "title": p.get("title", "") or "",
        "company": org.get("name", "") or "",
        "company_domain": org.get("primary_domain", "") or org.get("website_url", "") or "",
        "company_size": str(org.get("estimated_num_employees", "") or ""),
        "industry": org.get("industry", "") or "",
        "company_desc": (org.get("short_description", "") or "")[:600],
        "apollo_id": p.get("id", "") or "",
        "email": p.get("email", "") or "",
    }


def preview_apollo(titles=None, size_ranges=None, keywords=None, locations=None,
                   pages=1, per_page=25) -> dict:
    """Search-only preview (NO credit spend). Shows who Apollo has for these
    filters and how many have an email, so you can decide before revealing."""
    result = {"total": 0, "with_email": 0, "people": [], "error": None}
    if not os.environ.get("APOLLO_API_KEY"):
        result["error"] = "APOLLO_API_KEY not set"
        return result
    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    try:
        for page in range(1, pages + 1):
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, per_page)
            people = data.get("people", []) or []
            if not people:
                break
            for p in people:
                org = p.get("organization") or {}
                he = bool(p.get("has_email"))
                result["total"] += 1
                if he:
                    result["with_email"] += 1
                result["people"].append({
                    "first_name": p.get("first_name", "") or "—",
                    "company": org.get("name", "") or "—",
                    "title": (p.get("title", "") or "—")[:64],
                    "has_email": he,
                })
    except Exception as e:
        result["error"] = str(e)
    return result


def pull_apollo(db, titles=None, size_ranges=None, keywords=None, locations=None,
                target=25, per_page=100, max_pages=25, do_reveal=True) -> dict:
    """Pull until `target` NEW unique leads are imported (or the pool runs out).

    Keeps fetching pages, skipping duplicates/no-email, and reveals emails only
    up to the target — so you reliably get the count you asked for without
    over-spending credits. Pre-skips already-known companies before enriching.
    """
    stats = {"target": target, "fetched": 0, "has_email": 0, "imported": 0,
             "no_email": 0, "skipped_suppressed": 0, "skipped_duplicate": 0,
             "skipped_domain_dupe": 0, "skipped_invalid": 0, "exhausted": False}
    if not os.environ.get("APOLLO_API_KEY"):
        log(db, "error", "apollo pull skipped: APOLLO_API_KEY not set")
        return stats

    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    existing_domains = {l.company_domain for l in
                        db.query(Lead.company_domain).all() if l.company_domain}
    existing_companies = {(l.company or "").strip().lower()
                          for l in db.query(Lead.company).all() if l.company}

    try:
        for page in range(1, max_pages + 1):
            if stats["imported"] >= target:
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, per_page)
            people = data.get("people", []) or []
            if not people:
                stats["exhausted"] = True          # ran out of matching contacts
                break
            for p in people:
                if stats["imported"] >= target:
                    break
                stats["fetched"] += 1
                # Skip anyone with no email on file — enriching them wastes a credit
                if not p.get("has_email"):
                    stats["no_email"] += 1
                    continue
                # Pre-skip a company we already have, from the FREE search data,
                # so we never pay a credit to reveal a duplicate company.
                org = p.get("organization") or {}
                cname = (org.get("name") or "").strip().lower()
                if cname and cname in existing_companies:
                    stats["skipped_domain_dupe"] += 1
                    continue

                pid = p.get("id", "")
                if not (do_reveal and pid):
                    continue
                stats["has_email"] += 1
                enriched = _enrich_person(pid)
                time.sleep(0.25)                   # be gentle on the rate limit
                f = _person_to_fields(enriched)
                email = f["email"].lower()
                if not _usable_email(email):
                    stats["no_email"] += 1
                    continue
                if email in suppressed:
                    stats["skipped_suppressed"] += 1
                    continue
                if email in existing_emails:
                    stats["skipped_duplicate"] += 1
                    continue
                if verify_email(email, do_mx=False) != "ok":
                    stats["skipped_invalid"] += 1
                    continue
                domain = normalize_domain(f["company_domain"]) or email.split("@", 1)[1]
                if domain in existing_domains:
                    stats["skipped_domain_dupe"] += 1
                    continue

                db.add(Lead(
                    email=email, first_name=f["first_name"], last_name=f["last_name"],
                    title=f["title"], company=f["company"], company_domain=domain,
                    company_size=f["company_size"], industry=f["industry"],
                    company_desc=f["company_desc"], trigger=f.get("trigger", ""),
                    source="apollo", status="verified", verify_result="ok",
                ))
                existing_emails.add(email)
                existing_domains.add(domain)
                if cname:
                    existing_companies.add(cname)
                stats["imported"] += 1
    except requests.HTTPError as e:
        log(db, "error", f"apollo pull HTTP error: {e}")
    except Exception as e:
        log(db, "error", f"apollo pull failed: {e}")

    log(db, "import", f"Apollo pull (target {target}): {stats}")
    db.commit()
    return stats
