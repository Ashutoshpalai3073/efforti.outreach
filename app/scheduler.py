"""Background jobs: process due sends + poll IMAP for replies and bounces.

Safety rails enforced here, in order:
  1. Mailbox active + under effective (warm-up-ramped) daily cap
  2. Lead not suppressed / not replied / not bounced
  3. Send only inside the lead's local business hours (default 09:00-17:00)
  4. Random jitter so sends don't fire in bursts
  5. Auto-pause any mailbox whose 7-day bounce rate exceeds the threshold
"""
import email as email_lib
import imaplib
import os
import random
from datetime import datetime, timedelta, timezone

from .emailer import send
from .models import (Enrollment, Event, Lead, Mailbox, Message, SessionLocal,
                     Suppression, log, utcnow)

BUSINESS_START = int(os.environ.get("BUSINESS_HOUR_START", "9"))
BUSINESS_END = int(os.environ.get("BUSINESS_HOUR_END", "17"))
BOUNCE_PAUSE_THRESHOLD = float(os.environ.get("BOUNCE_PAUSE_THRESHOLD", "0.03"))
JITTER_MAX_MIN = int(os.environ.get("JITTER_MAX_MINUTES", "45"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

BOUNCE_SUBJECT_MARKERS = ("delivery status notification", "undeliverable",
                          "mail delivery failed", "returned mail",
                          "delivery incomplete", "failure notice")


def in_business_hours(lead: Lead, now_utc: datetime) -> bool:
    local = now_utc + timedelta(hours=lead.timezone_offset or 5.5)
    if local.weekday() >= 5:  # Sat/Sun
        return False
    return BUSINESS_START <= local.hour < BUSINESS_END


def roll_daily_counters(db, mailbox: Mailbox, today: str):
    if mailbox.sent_today_date != today:
        mailbox.sent_today = 0
        mailbox.sent_today_date = today


def process_due_sends():
    """Runs every few minutes. Sends whatever is due and allowed."""
    db = SessionLocal()
    try:
        now = utcnow()
        today = now.strftime("%Y-%m-%d")
        due = (db.query(Enrollment)
               .filter(Enrollment.status == "active",
                       Enrollment.next_send_at <= now)
               .order_by(Enrollment.next_send_at)
               .limit(50).all())

        suppressed = {s.email for s in db.query(Suppression).all()}

        for enr in due:
            lead, mailbox = enr.lead, enr.mailbox
            if lead.email in suppressed or lead.status in (
                    "replied", "bounced", "unsubscribed"):
                enr.status = "halted_manual"
                continue

            if not mailbox or not mailbox.active:
                enr.next_send_at = now + timedelta(hours=1)
                continue

            roll_daily_counters(db, mailbox, today)
            if mailbox.sent_today >= mailbox.effective_cap():
                enr.next_send_at = now + timedelta(hours=3)
                continue

            if not in_business_hours(lead, now):
                enr.next_send_at = now + timedelta(minutes=random.randint(30, 90))
                continue

            steps = enr.sequence.steps
            if enr.current_step >= len(steps):
                enr.status = "finished"
                continue
            step = steps[enr.current_step]

            ok = send(db, mailbox, lead, enr, step.subject, step.body,
                      enr.current_step)
            if ok:
                mailbox.sent_today += 1
                mailbox.sends_7d += 1
                lead.status = "contacted"
                enr.current_step += 1
                if enr.current_step >= len(steps):
                    enr.status = "finished"
                    lead.status = "finished" if lead.status == "contacted" \
                        else lead.status
                else:
                    nxt = steps[enr.current_step]
                    enr.next_send_at = (now + timedelta(days=nxt.wait_days)
                                        + timedelta(minutes=random.randint(
                                            0, JITTER_MAX_MIN)))
            else:
                enr.next_send_at = now + timedelta(hours=6)  # retry later
        db.commit()
    finally:
        db.close()


def _classify_inbound(msg) -> str:
    subject = (msg.get("Subject") or "").lower()
    from_addr = (msg.get("From") or "").lower()
    if any(m in subject for m in BOUNCE_SUBJECT_MARKERS) or \
            "mailer-daemon" in from_addr or "postmaster" in from_addr:
        return "bounce"
    return "reply"


def _extract_target_email(msg, kind: str) -> str:
    """For a reply, sender is the lead. For a bounce, find the failed rcpt."""
    if kind == "reply":
        from_header = msg.get("From") or ""
        addr = email_lib.utils.parseaddr(from_header)[1]
        return addr.lower()
    # bounce: look for original To in the DSN payload
    try:
        for part in msg.walk():
            if part.get_content_type() in ("message/delivery-status",
                                           "text/plain"):
                payload = part.get_payload(decode=True) or b""
                text = payload.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    low = line.lower()
                    if low.startswith(("final-recipient:", "original-recipient:")):
                        return line.split(";")[-1].strip().lower()
    except Exception:
        pass
    return ""


def poll_inboxes():
    """Runs every N minutes. Checks each mailbox for replies and bounces."""
    if DRY_RUN:
        return  # nothing real was sent; nothing to poll
    db = SessionLocal()
    try:
        known_leads = {l.email: l for l in db.query(Lead).all()}
        for mailbox in db.query(Mailbox).filter(Mailbox.active.is_(True)).all():
            try:
                imap = imaplib.IMAP4_SSL(mailbox.imap_host)
                imap.login(mailbox.email, mailbox.app_password)
                imap.select("INBOX")
                _, data = imap.search(None, "UNSEEN")
                for num in (data[0].split() if data and data[0] else []):
                    _, msg_data = imap.fetch(num, "(RFC822)")
                    msg = email_lib.message_from_bytes(msg_data[0][1])
                    kind = _classify_inbound(msg)
                    target = _extract_target_email(msg, kind)
                    lead = known_leads.get(target)
                    if not lead:
                        continue
                    if kind == "bounce":
                        lead.status = "bounced"
                        db.add(Suppression(email=lead.email, reason="bounced"))
                        mailbox.bounces_7d += 1
                        _halt(db, lead, "halted_bounce")
                        log(db, "bounce", f"{lead.email} bounced "
                                          f"(via {mailbox.email})")
                    else:
                        lead.status = "replied"
                        _halt(db, lead, "halted_reply")
                        log(db, "reply", f"REPLY from {lead.email} — sequence "
                                         f"stopped. Check {mailbox.email} inbox.")
                imap.logout()
            except Exception as e:
                log(db, "error", f"IMAP poll failed for {mailbox.email}: {e}")

            # Auto-pause on bounce rate
            if mailbox.sends_7d >= 30 and \
                    mailbox.bounce_rate() > BOUNCE_PAUSE_THRESHOLD:
                mailbox.active = False
                mailbox.paused_reason = (
                    f"auto-paused: bounce rate "
                    f"{mailbox.bounce_rate():.1%} > "
                    f"{BOUNCE_PAUSE_THRESHOLD:.0%}")
                log(db, "pause", f"{mailbox.email} {mailbox.paused_reason}")
        db.commit()
    finally:
        db.close()


def _halt(db, lead: Lead, status: str):
    for enr in db.query(Enrollment).filter(
            Enrollment.lead_id == lead.id,
            Enrollment.status == "active").all():
        enr.status = status


def weekly_counter_decay():
    """Rough 7-day rolling window: decay counters daily by 1/7."""
    db = SessionLocal()
    try:
        for mb in db.query(Mailbox).all():
            mb.sends_7d = int(mb.sends_7d * 6 / 7)
            mb.bounces_7d = int(mb.bounces_7d * 6 / 7)
        db.commit()
    finally:
        db.close()
