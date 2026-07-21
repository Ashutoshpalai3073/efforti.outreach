# Efforti Outreach Engine

Self-hosted cold-email engine: CSV import → verification → multi-touch sequences
→ reply/bounce detection → suppression. One Python process, SQLite, no external
services. Built for ~300–1,300 emails/week across a handful of Gmail mailboxes.

## What it does

- **Import** Apollo CSV exports. Filters bad syntax, dead domains (MX check),
  suppressed emails, duplicates, and enforces one lead per company domain.
- **Sequences** — 3-touch by default (Day 0 / +3 / +5). Follow-ups thread as
  "Re:" replies via In-Reply-To headers. Jinja2 personalization:
  `{{first_name}}`, `{{company}}`, `{{title}}`, `{{trigger}}`.
- **Sends safely**: per-mailbox daily caps with a warm-up ramp (8/day, +2/day,
  up to cap), business-hours-only in the lead's timezone, weekdays only,
  randomized jitter, round-robin across mailboxes.
- **Listens**: IMAP polling every 10 min. A reply halts the sequence and
  surfaces on the dashboard. A bounce suppresses the lead. If a mailbox's
  7-day bounce rate exceeds 3%, it auto-pauses.
- **Compliance**: one-click unsubscribe endpoint (`/u/{token}`),
  List-Unsubscribe headers, permanent suppression list checked at import,
  enroll, and send time.
- **DRY_RUN mode** (default ON): full pipeline runs, every "send" is logged
  to the Activity page, nothing leaves the machine.

## Quick start (local, dry run)

```bash
pip install -r requirements.txt
./run.sh
# open http://localhost:8000
```

1. Mailboxes → add a mailbox (any fake password works in dry run).
2. Leads → import a CSV (Apollo export format; needs an Email column).
3. Leads → "Enroll all verified" into the seeded Efforti sequence.
4. Dashboard → "Run send cycle now" → check Activity for [DRY RUN] sends.

## Going live — do these IN ORDER, skipping steps burns your domains

1. **Buy 2–3 lookalike domains** (getefforti.com, tryefforti.com).
   NEVER send cold email from efforti.ai.
2. **Google Workspace** on each domain, 2–3 mailboxes per domain.
3. **DNS**: SPF, DKIM, DMARC (`p=none` to start) on every sending domain.
   Verify with `dig TXT yourdomain.com` and Google Admin's toolbox.
4. **Warm up 2–3 weeks** before real volume. This tool ramps automatically
   (8/day → cap), but brand-new domains benefit from a warm-up network
   first (Smartlead/Instantly warm-up alone costs almost nothing).
5. **App passwords**: each mailbox needs 2FA on, then Google Account →
   Security → App passwords. Paste into Mailboxes page.
6. **Deploy** on a small VPS with a public URL (unsubscribe links must
   resolve). Set in `.env`:
   ```
   DRY_RUN=false
   APP_BASE_URL=https://your-public-host
   ```
   Run behind nginx/caddy with HTTPS. Add basic auth or IP-restrict the
   dashboard — only `/u/{token}` should be public.
7. **Register domains in Google Postmaster Tools** and watch reputation.

## Operating rhythm (weekly, ~1 hour)

- Import next batch (300–400 fresh leads max/week).
- Check Dashboard replies daily — reply within hours, that's where deals are.
- Watch bounce rates; anything auto-paused stays paused until you know why.
- Kill or rewrite any step with <0.5% positive replies after ~200 sends.
- Log a loss reason for every "no" — that data is worth more than the yes's.

## Legal notes (not legal advice)

- India has no dedicated cold-B2B-email statute, but honor every opt-out
  immediately (the suppression list does this).
- If emailing US leads: CAN-SPAM requires a truthful From, no deceptive
  subject, a working unsubscribe (built in), and a physical postal address —
  **add your company address to the sequence footer before going live**.
- EU/UK leads: PECR/GDPR make cold email to individuals risky; B2B corporate
  addresses are more defensible, but get advice before targeting Europe.

## Architecture

```
app/
  main.py       FastAPI routes + server-rendered UI (Jinja2)
  models.py     SQLAlchemy: Mailbox, Lead, Sequence, Enrollment, Message,
                Suppression, Event
  importer.py   CSV import + syntax/MX verification + dedupe
  emailer.py    SMTP send, threading headers, unsubscribe footer, dry-run
  scheduler.py  APScheduler jobs: due sends (5 min), IMAP poll (10 min),
                counter decay (daily)
  seed.py       Default Efforti 3-touch sequence
  ui/           Templates
```

Deliberate simplifications vs a SaaS tool: SQLite (fine to ~100K leads),
single process, app-password SMTP instead of OAuth, no open tracking
(pixels hurt deliverability and lie anyway — replies are the metric).

## Upgrade path

- Postgres: change one line in `models.py` (`create_engine(...)`).
- Better verification: plug ZeroBounce into `importer.verify_email`.
- More mailboxes: just add them; enrollment round-robins automatically.
- Apollo API auto-pull, per-step reply analytics, AI-personalized first
  lines: all clean extensions of the current schema.
