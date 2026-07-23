"""Live per-company research that powers genuinely personalized outreach.

The difference between this and enrich.py: enrich turns *known* Apollo facts into
a one-liner. This goes out and *finds* current, specific facts about the company —
what they do, recent moves (funding, launches, hiring), and the operational pain a
scaling SMB CEO feels — using Claude with the web-search tool. The result is a
tight briefing (not a thesis), cached per company and reused by every lead there,
then fed into the opener so the first line references something real and current.

Provider: ANTHROPIC_API_KEY -> Claude with web search. No key -> nothing runs
(the UI says so loudly). If web search isn't available on the account, we fall
back to a Claude summary of the Apollo facts so the pipeline never hard-fails.
"""
import os

from .models import Lead, log, utcnow

# Web search + strong reasoning. Sonnet 5 supports the dynamic-filtering web tool
# and is the cost-effective choice for volume research. Override via env.
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "claude-sonnet-5")
# Newer dynamic-filtering web search (Sonnet 5 / Opus 4.x); we fall back to the
# basic tool, then to no-web, so older accounts still work.
WEB_TOOLS = ("web_search_20260209", "web_search_20250305")
MAX_SEARCHES = int(os.environ.get("RESEARCH_MAX_SEARCHES", "4"))   # per company
MAX_COMPANIES = int(os.environ.get("RESEARCH_MAX_COMPANIES", "60"))  # per run, cost cap

RESEARCH_SYSTEM = """You are a B2B research analyst prepping a cold email for Efforti \
— an AI leadership assistant that gives SMB CEOs live visibility into where their \
team's effort actually goes (blockers, risks, misalignment) as they scale.

Research ONE company using web search and return a TIGHT briefing a salesperson can \
skim in 10 seconds — specifics, not a thesis. Rules:
- Output ONLY the bullet lines. No preamble, no meta-comment about the data or what \
the bullets show, no sign-off, no headers. Your entire reply is the bullets.
- Only real facts you actually found. Never invent. If the web turns up almost \
nothing, return a single bullet saying so — do not pad.
- 3–5 short bullets, max ~90 words total.
- Prioritize: what they do / their product, stage & size signals, a RECENT move \
(funding, launch, big hire, expansion, notable customer), and one concrete \
operational pain a CEO likely feels as this specific company scales.
- Each line starts with "- ". Plain text. No links, no markdown."""

NOWEB_SYSTEM = """You are a B2B research analyst prepping a cold email for Efforti \
(an AI leadership assistant giving SMB CEOs visibility into team effort as they scale). \
You have no live web access — infer a tight briefing ONLY from the facts provided. \
3–4 short bullets, ~70 words, plain "- " lines, no invented specifics, no markdown."""


def _facts(lead: Lead) -> str:
    bits = [f"Company: {lead.company or 'unknown'}"]
    if lead.company_domain:
        bits.append(f"Website/domain: {lead.company_domain}")
    if lead.industry:
        bits.append(f"Industry: {lead.industry}")
    if lead.company_size:
        bits.append(f"Headcount: {lead.company_size}")
    if lead.trigger:
        bits.append(f"Known signal: {lead.trigger}")
    if lead.company_desc:
        bits.append(f"Blurb: {lead.company_desc}")
    return "\n".join(bits)


def _text(resp) -> str:
    # Join with newlines, not "": web search emits several text blocks, and ""
    # would fuse the end of one bullet to the start of the next on one line.
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def _clean_briefing(text: str) -> str:
    """With web search, the model narrates its reasoning between searches in text
    blocks ("Let me check hiring news…"). The briefing itself is the bullet list,
    so drop everything before the first bullet and keep only bullet lines."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith("-")),
                 None)
    if start is None:
        return text.strip()          # no bullets — keep as-is (rare/empty case)
    kept = [l for l in lines[start:] if l.strip()]
    return "\n".join(kept).strip()


def _research_with_web(client, lead: Lead) -> str:
    """One company, using web search. Tries the newer tool then the basic one.
    Handles the server-tool pause_turn loop. Returns '' if web search is
    unavailable so the caller can fall back."""
    prompt = (f"Research this company for a cold email.\n\n{_facts(lead)}\n\n"
              "Search the web for current, specific facts and write the briefing.")
    last_err = None
    for tool_type in WEB_TOOLS:
        tools = [{"type": tool_type, "name": "web_search", "max_uses": MAX_SEARCHES}]
        messages = [{"role": "user", "content": prompt}]
        try:
            for _ in range(6):          # allow server-tool continuations
                resp = client.messages.create(
                    model=RESEARCH_MODEL, max_tokens=1500,
                    system=RESEARCH_SYSTEM, tools=tools, messages=messages)
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                return _clean_briefing(_text(resp))
            return ""                    # ran out of continuations
        except Exception as e:           # tool type not supported / no web access
            last_err = e
            continue
    if last_err:
        raise last_err
    return ""


def _research_from_facts(client, lead: Lead) -> str:
    """No-web fallback: summarize the Apollo facts into a briefing so a run never
    hard-fails when web search is unavailable."""
    resp = client.messages.create(
        model=RESEARCH_MODEL, max_tokens=500, system=NOWEB_SYSTEM,
        messages=[{"role": "user", "content": _facts(lead)}])
    return _clean_briefing(_text(resp))


def research_companies(db, limit: int = MAX_COMPANIES, refresh: bool = False) -> dict:
    """Research each distinct company among verified/enrolled leads and cache the
    briefing on every lead at that company. `refresh=True` re-researches even
    companies that already have a briefing. Deduping by company is the main cost
    lever — 10 leads at one company cost one research, not ten."""
    stats = {"provider": "none", "companies": 0, "leads": 0,
             "web": 0, "noweb": 0, "failed": 0, "skipped_no_company": 0}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        stats["provider"] = "fallback-only"
        return stats

    import anthropic
    client = anthropic.Anthropic()
    stats["provider"] = "anthropic"

    q = db.query(Lead).filter(Lead.status.in_(["verified", "enrolled"]))
    if not refresh:
        q = q.filter((Lead.company_research == "") |
                     (Lead.company_research.is_(None)))
    leads = q.limit(4000).all()

    groups = {}                          # company-key -> [leads]
    for lead in leads:
        key = (lead.company_domain or lead.company or "").strip().lower()
        if not key:
            stats["skipped_no_company"] += 1
            continue
        groups.setdefault(key, []).append(lead)

    for key, group in list(groups.items())[:limit]:
        rep = group[0]
        briefing, used_web = "", False
        try:
            briefing = _research_with_web(client, rep)
            used_web = bool(briefing)
        except Exception as e:
            log(db, "research", f"web research failed for {rep.company}: {e}")
        if not briefing:                 # fall back to fact-only summary
            try:
                briefing = _research_from_facts(client, rep)
            except Exception as e:
                log(db, "error", f"research failed for {rep.company}: {e}")
        if not briefing:
            stats["failed"] += 1
            continue
        now = utcnow()
        for lead in group:
            lead.company_research = briefing
            lead.researched_at = now
            stats["leads"] += 1
        stats["companies"] += 1
        stats["web" if used_web else "noweb"] += 1

    db.commit()
    log(db, "research", f"Company research ({stats['provider']}): {stats}")
    return stats
