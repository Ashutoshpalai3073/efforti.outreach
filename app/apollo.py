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

# Default Efforti ICP — the top 5 decision-makers per company, ranked by fit for
# a "live visibility into team effort/blockers/risks" product:
#   1 CEO/Founder (economic buyer)  2 COO (owns execution)
#   3 Chief of Staff (visibility champion)  4 CFO (ROI angle)
#   5 CPO (product/team delivery)
# Title strings include the common variants Apollo matches on.
DEFAULT_TITLES = [
    "CEO", "Chief Executive Officer", "Founder", "Co-Founder", "Owner",
    "COO", "Chief Operating Officer",
    "Chief of Staff",
    "CFO", "Chief Financial Officer",
    "CPO", "Chief Product Officer",
]
# Seniority bias so we only ever pull genuinely senior people, never a random
# "manager" who happens to have one of the title words.
DEFAULT_SENIORITIES = ["owner", "founder", "c_suite"]
DEFAULT_SIZE_RANGES = ["21,50", "51,100", "101,200"]

# Named headcount presets for the UI dropdown -> Apollo range strings.
SIZE_PRESETS = {
    "seed":       ["1,10", "11,20"],
    "startup":    ["21,50", "51,100", "101,200"],   # default ICP: 30–200
    "growth":     ["201,500", "501,1000"],
    "midmarket":  ["1001,2000", "2001,5000"],
    "any":        ["1,10", "11,20", "21,50", "51,100", "101,200",
                   "201,500", "501,1000"],
}

LOCKED_EMAIL_MARKERS = ("not_unlocked", "email_not_unlocked", "domain.com")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", ""),
    }


def _org_domain(org: dict) -> str:
    """Best-effort company domain from a (free) search-result org object, so we
    can group by brand and enforce the per-brand cap BEFORE spending a credit."""
    raw = org.get("primary_domain") or org.get("website_url") or ""
    return normalize_domain(raw) if raw else ""


def _search_page(titles, size_ranges, keywords, locations, page, per_page,
                 seniorities=None):
    body = {
        "person_titles": titles,
        "person_seniorities": seniorities or DEFAULT_SENIORITIES,
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
                   seniorities=None, brands=20, per_brand=5,
                   max_pages=15) -> dict:
    """Search-only preview (NO credit spend). Groups results BY BRAND and keeps
    up to `per_brand` top execs per company, across up to `brands` companies —
    so you see exactly the shape of what a pull would import, and how many
    reveals (credits) it would cost, before spending anything."""
    result = {"brands_found": 0, "contacts": 0, "with_email": 0,
              "brands": [], "error": None,
              "want_brands": brands, "want_per_brand": per_brand}
    if not os.environ.get("APOLLO_API_KEY"):
        result["error"] = "APOLLO_API_KEY not set"
        return result
    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES
    grouped = {}      # domain -> {"company", "people":[...]}
    try:
        for page in range(1, max_pages + 1):
            if len(grouped) >= brands and \
                    all(len(g["people"]) >= per_brand for g in grouped.values()):
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities)
            people = data.get("people", []) or []
            if not people:
                break
            for p in people:
                org = p.get("organization") or {}
                dom = _org_domain(org) or (org.get("name") or "").strip().lower()
                if not dom:
                    continue
                if dom not in grouped:
                    if len(grouped) >= brands:
                        continue                 # brand scope reached
                    grouped[dom] = {"company": org.get("name") or "—",
                                    "people": []}
                g = grouped[dom]
                if len(g["people"]) >= per_brand:
                    continue                     # this brand is full
                he = bool(p.get("has_email"))
                g["people"].append({
                    "first_name": p.get("first_name", "") or "—",
                    "title": (p.get("title", "") or "—")[:64],
                    "has_email": he,
                })
                result["contacts"] += 1
                if he:
                    result["with_email"] += 1
    except Exception as e:
        result["error"] = str(e)
    result["brands_found"] = len(grouped)
    result["brands"] = sorted(grouped.values(),
                              key=lambda g: g["company"].lower())
    return result


def pull_apollo(db, titles=None, size_ranges=None, keywords=None, locations=None,
                seniorities=None, brands=20, per_brand=5,
                max_pages=40, do_reveal=True) -> dict:
    """Multi-thread pull: import up to `per_brand` top execs at up to `brands`
    companies (default 5 execs × 20 brands = 100 targets).

    Instead of one random person per company, this deliberately gathers the
    decision-making unit at each brand. It enforces a per-brand cap (counting
    execs already in the DB from earlier pulls) and a brand-count cap, so the
    scope is exactly `brands × per_brand`, never a random total. Reveals cost
    ~1 Apollo credit each and only happen for contacts that pass the free-data
    gates first.
    """
    target_total = brands * per_brand
    stats = {"brands": brands, "per_brand": per_brand,
             "target_total": target_total, "fetched": 0, "has_email": 0,
             "imported": 0, "brands_filled": 0, "no_email": 0,
             "skipped_suppressed": 0, "skipped_duplicate": 0,
             "skipped_brand_full": 0, "skipped_scope": 0,
             "skipped_invalid": 0, "exhausted": False}
    if not os.environ.get("APOLLO_API_KEY"):
        log(db, "error", "apollo pull skipped: APOLLO_API_KEY not set")
        return stats

    titles = titles or DEFAULT_TITLES
    size_ranges = size_ranges or DEFAULT_SIZE_RANGES

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    # How many execs we already hold per domain — so topping a brand up never
    # exceeds per_brand across separate pulls.
    domain_counts = {}
    for l in db.query(Lead.company_domain).all():
        if l.company_domain:
            domain_counts[l.company_domain] = domain_counts.get(
                l.company_domain, 0) + 1
    brand_domains = {}   # domains touched THIS run -> count added this run

    def _room(dom: str) -> bool:
        """True if this domain still has capacity AND fits the brand scope."""
        held = domain_counts.get(dom, 0) + brand_domains.get(dom, 0)
        if held >= per_brand:
            return False
        if dom not in brand_domains and len(brand_domains) >= brands:
            return False
        return True

    try:
        for page in range(1, max_pages + 1):
            if stats["imported"] >= target_total:
                break
            data = _search_page(titles, size_ranges, keywords, locations,
                                page, 100, seniorities)
            people = data.get("people", []) or []
            if not people:
                stats["exhausted"] = True          # ran out of matching contacts
                break
            for p in people:
                if stats["imported"] >= target_total:
                    break
                stats["fetched"] += 1
                if not p.get("has_email"):
                    stats["no_email"] += 1
                    continue
                org = p.get("organization") or {}
                sdom = _org_domain(org)
                # Pre-skip on the FREE search domain when we already know this
                # brand is full or out of scope — saves a wasted credit.
                if sdom and not _room(sdom):
                    held = domain_counts.get(sdom, 0) + brand_domains.get(sdom, 0)
                    if held >= per_brand:
                        stats["skipped_brand_full"] += 1
                    else:
                        stats["skipped_scope"] += 1
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
                domain = normalize_domain(f["company_domain"]) or \
                    email.split("@", 1)[1]
                if not _room(domain):
                    held = domain_counts.get(domain, 0) + \
                        brand_domains.get(domain, 0)
                    if held >= per_brand:
                        stats["skipped_brand_full"] += 1
                    else:
                        stats["skipped_scope"] += 1
                    continue

                db.add(Lead(
                    email=email, first_name=f["first_name"], last_name=f["last_name"],
                    title=f["title"], company=f["company"], company_domain=domain,
                    company_size=f["company_size"], industry=f["industry"],
                    company_desc=f["company_desc"], trigger=f.get("trigger", ""),
                    source="apollo", status="verified", verify_result="ok",
                ))
                existing_emails.add(email)
                brand_domains[domain] = brand_domains.get(domain, 0) + 1
                stats["imported"] += 1
    except requests.HTTPError as e:
        log(db, "error", f"apollo pull HTTP error: {e}")
    except Exception as e:
        log(db, "error", f"apollo pull failed: {e}")

    stats["brands_filled"] = len(brand_domains)
    log(db, "import",
        f"Apollo pull: imported {stats['imported']} execs across "
        f"{stats['brands_filled']} brands (target {per_brand}×{brands}"
        f"={target_total}) · {stats['fetched']} scanned · "
        f"{stats['skipped_brand_full']} brand-full · "
        f"{stats['skipped_duplicate']} duplicate · "
        f"{stats['no_email']} without email"
        + (" · pool exhausted" if stats["exhausted"] else ""))
    db.commit()
    return stats
