"""CSV import (Apollo export compatible) + lightweight email verification."""
import csv
import io
import re

import dns.resolver

from .models import Lead, Suppression, log

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Apollo export header -> our field. Lowercased, spaces kept.
COLUMN_MAP = {
    "email": "email",
    "first name": "first_name",
    "last name": "last_name",
    "title": "title",
    "company": "company",
    "company name": "company",
    "company name for emails": "company",
    "# employees": "company_size",
    "company size": "company_size",
    "website": "company_domain",
    "company website": "company_domain",
    "trigger": "trigger",
    "source": "source",
}

# Fields we care about, in priority order. `email` is required; the rest make
# personalization better. Used to validate an uploaded file's header row.
REQUIRED_FIELDS = ["email"]
RECOMMENDED_FIELDS = ["first_name", "company", "title", "trigger"]

_mx_cache: dict[str, bool] = {}


def has_mx(domain: str) -> bool:
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        ok = len(answers) > 0
    except Exception:
        ok = False
    _mx_cache[domain] = ok
    return ok


def verify_email(email: str, do_mx: bool = True) -> str:
    """Returns ok / bad_syntax / no_mx. Cheap tier — plug ZeroBounce here later."""
    if not EMAIL_RE.match(email):
        return "bad_syntax"
    if do_mx and not has_mx(email.split("@", 1)[1]):
        return "no_mx"
    return "ok"


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = re.sub(r"^www\.", "", v)
    return v.split("/")[0]


def inspect_headers(headers: list) -> dict:
    """Map a file's header row to our fields. Returns what was detected,
    what's missing, and whether the required Email column is present."""
    detected = {}   # our_field -> original header text
    for h in headers or []:
        key = COLUMN_MAP.get((h or "").strip().lower())
        if key and key not in detected:
            detected[key] = h
    return {
        "headers": [h for h in (headers or []) if h],
        "detected": detected,
        "detected_fields": sorted(detected.keys()),
        "missing_required": [f for f in REQUIRED_FIELDS if f not in detected],
        "missing_recommended": [f for f in RECOMMENDED_FIELDS if f not in detected],
        "has_email": "email" in detected,
    }


def import_csv(db, file_bytes: bytes, do_mx: bool = True) -> dict:
    """Import leads. First validates the header row (an Email column is
    required); then enforces valid syntax, MX exists, not suppressed, not
    already present, one lead per company domain."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    hdr = inspect_headers(reader.fieldnames)

    stats = {"imported": 0, "skipped_invalid": 0, "skipped_suppressed": 0,
             "skipped_duplicate": 0, "skipped_domain_dupe": 0, "error": None}
    stats.update(hdr)

    # Hard stop: no Email column means we can't send anything.
    if not hdr["has_email"]:
        stats["error"] = "no_email_column"
        log(db, "import",
            f"CSV rejected — no Email column. Found headers: {hdr['headers']}")
        db.commit()
        return stats

    suppressed = {s.email for s in db.query(Suppression).all()}
    existing_emails = {l.email for l in db.query(Lead.email).all()}
    existing_domains = {l.company_domain for l in
                        db.query(Lead.company_domain).all() if l.company_domain}

    for row in reader:
        data = {}
        for col, val in row.items():
            key = COLUMN_MAP.get((col or "").strip().lower())
            if key and val:
                data[key] = val.strip()
        email = data.get("email", "").lower()
        if not email:
            stats["skipped_invalid"] += 1
            continue
        if email in suppressed:
            stats["skipped_suppressed"] += 1
            continue
        if email in existing_emails:
            stats["skipped_duplicate"] += 1
            continue

        result = verify_email(email, do_mx=do_mx)
        if result != "ok":
            stats["skipped_invalid"] += 1
            continue

        domain = normalize_domain(data.get("company_domain", "")) or \
            email.split("@", 1)[1]
        if domain in existing_domains:
            stats["skipped_domain_dupe"] += 1
            continue

        db.add(Lead(
            email=email,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            title=data.get("title", ""),
            company=data.get("company", ""),
            company_domain=domain,
            company_size=data.get("company_size", ""),
            source=data.get("source", "csv"),
            trigger=data.get("trigger", ""),
            status="verified",
            verify_result=result,
        ))
        existing_emails.add(email)
        existing_domains.add(domain)
        stats["imported"] += 1

    skipped = (stats["skipped_invalid"] + stats["skipped_suppressed"]
               + stats["skipped_duplicate"] + stats["skipped_domain_dupe"])
    log(db, "import", f"CSV import: {stats['imported']} imported, {skipped} "
                      f"skipped. Columns: {stats['detected_fields']}")
    db.commit()
    return stats
