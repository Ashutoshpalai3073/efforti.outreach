"""AI-written personalized opener per lead.

For each lead we generate ONE short, specific first line that references
something true about their company — so the cold email reads as written for
them, not blasted. Provider is auto-detected:

  * GROQ_API_KEY set      -> Groq (Llama, OpenAI-compatible) — free/cheap
  * ANTHROPIC_API_KEY set -> Claude (Haiku by default)
  * neither               -> safe generic fallback line (pipeline never blocks)
"""
import os

import requests

from .models import Lead, log

# Groq: fast, free tier, good enough for a one-liner. Override with GROQ_MODEL.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Anthropic: cheapest capable model for a one-line-per-lead task at volume.
OPENER_MODEL = os.environ.get("OPENER_MODEL", "claude-haiku-4-5")

SYSTEM = """You write the opening line of a cold sales email for Efforti — an AI \
leadership assistant that gives SMB CEOs live visibility into where their team's \
effort actually goes (blockers, risks, misalignment) without adding another tool.

You are given facts about ONE prospect. Write a SINGLE opening line (max 30 words) \
that references one specific, true detail about their company (growth, funding, \
headcount, distributed team, industry) and connects it to the pain of a CEO \
losing visibility as the team scales. Rules:
- One sentence. No greeting ("Hi X" is added separately). No sign-off.
- Concrete and specific to THIS company. Never generic flattery.
- Never invent facts not given. If facts are thin, lead with the role/industry.
- Plain text. No emojis, no quotes around the line."""

FALLBACK = ("Quick question — as {company} scales, how long before you actually "
            "hear when a project starts slipping?")


def _prompt(lead: Lead) -> str:
    facts = [f"Company: {lead.company or 'unknown'}"]
    if lead.first_name:
        facts.append(f"Contact: {lead.first_name} {lead.last_name}".strip())
    if lead.title:
        facts.append(f"Title: {lead.title}")
    if lead.industry:
        facts.append(f"Industry: {lead.industry}")
    if lead.company_size:
        facts.append(f"Headcount: {lead.company_size}")
    if lead.trigger:
        facts.append(f"Recent signal: {lead.trigger}")
    if lead.company_desc:
        facts.append(f"About the company: {lead.company_desc}")
    research = (getattr(lead, "company_research", "") or "").strip()
    if research:
        facts.append(
            "Live research (current & specific — PREFER a concrete detail from "
            f"here for the opener):\n{research}")
    return "Prospect facts:\n" + "\n".join(facts) + "\n\nWrite the opening line."


def _clean(text: str) -> str:
    text = (text or "").strip().strip('"').strip()
    return text if (text and len(text) <= 320) else ""


def _groq_opener(lead: Lead) -> str:
    try:
        r = requests.post(GROQ_URL, timeout=30, headers={
            "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
            "Content-Type": "application/json",
        }, json={
            "model": GROQ_MODEL,
            "max_tokens": 120,
            "temperature": 0.7,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _prompt(lead)}],
        })
        r.raise_for_status()
        return _clean(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return ""


def _claude_opener(client, lead: Lead) -> str:
    try:
        resp = client.messages.create(
            model=OPENER_MODEL, max_tokens=120, system=SYSTEM,
            messages=[{"role": "user", "content": _prompt(lead)}],
        )
        return _clean(next((b.text for b in resp.content if b.type == "text"), ""))
    except Exception:
        return ""


def _fallback(lead: Lead) -> str:
    return FALLBACK.format(company=lead.company or "your company")


def enrich_leads(db, limit: int = 500) -> dict:
    """Fill in openers for verified leads that don't have one yet."""
    stats = {"provider": "none", "enriched": 0, "fallback": 0}

    provider = None
    claude_client = None
    if os.environ.get("GROQ_API_KEY"):
        provider = "groq"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        provider = "anthropic"
        import anthropic
        claude_client = anthropic.Anthropic()
    stats["provider"] = provider or "fallback-only"

    leads = (db.query(Lead)
             .filter(Lead.status == "verified",
                     (Lead.opener == "") | (Lead.opener.is_(None)))
             .limit(limit).all())
    for lead in leads:
        if provider == "groq":
            opener = _groq_opener(lead)
        elif provider == "anthropic":
            opener = _claude_opener(claude_client, lead)
        else:
            opener = ""
        if opener:
            stats["enriched"] += 1
        else:
            opener = _fallback(lead)
            stats["fallback"] += 1
        lead.opener = opener
    db.commit()
    log(db, "enrich", f"AI openers ({stats['provider']}): {stats}")
    return stats
