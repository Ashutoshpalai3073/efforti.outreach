"""SMTP sending with proper threading, unsubscribe headers, and dry-run mode."""
import os
import smtplib
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from jinja2 import Template

from .models import Enrollment, Lead, Mailbox, Message, log, utcnow

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def render(template_str: str, lead: Lead) -> str:
    return Template(template_str).render(
        first_name=lead.first_name or "there",
        last_name=lead.last_name,
        title=lead.title,
        company=lead.company or "your company",
        trigger=lead.trigger,
        industry=getattr(lead, "industry", ""),
        opener=getattr(lead, "opener", "") or "",
    )


def make_message_id(mailbox: Mailbox) -> str:
    domain = mailbox.email.split("@", 1)[1]
    return f"<{uuid.uuid4().hex}@{domain}>"


def build_email(mailbox: Mailbox, lead: Lead, enrollment: Enrollment,
                subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg_id = make_message_id(mailbox)
    msg["Message-ID"] = msg_id
    msg["Date"] = formatdate(localtime=False)
    msg["From"] = f"{mailbox.display_name} <{mailbox.email}>" \
        if mailbox.display_name else mailbox.email
    msg["To"] = lead.email

    if enrollment.current_step == 0 or not enrollment.thread_message_id:
        msg["Subject"] = subject
        enrollment.thread_message_id = msg_id
        enrollment.thread_subject = subject
    else:
        # Follow-up: same thread. Re: subject + threading headers.
        msg["Subject"] = "Re: " + (enrollment.thread_subject or subject)
        msg["In-Reply-To"] = enrollment.thread_message_id
        msg["References"] = enrollment.thread_message_id

    unsub_url = f"{APP_BASE_URL}/u/{lead.unsub_token}"
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    footer = (f"\n\n--\nIf you'd rather not hear from me, one click opts you "
              f"out: {unsub_url}")
    msg.set_content(body + footer)
    return msg, msg_id


def send(db, mailbox: Mailbox, lead: Lead, enrollment: Enrollment,
         subject_tpl: str, body_tpl: str, step_index: int) -> bool:
    subject = render(subject_tpl, lead) if subject_tpl else ""
    body = render(body_tpl, lead)
    msg, msg_id = build_email(mailbox, lead, enrollment, subject, body)

    record = Message(
        enrollment_id=enrollment.id, lead_email=lead.email,
        mailbox_email=mailbox.email, step_index=step_index,
        subject=msg["Subject"], body=body, message_id=msg_id,
        dry_run=DRY_RUN,
    )

    if DRY_RUN:
        record.status = "dry_run"
        db.add(record)
        log(db, "send", f"[DRY RUN] step {step_index} -> {lead.email} "
                        f"via {mailbox.email} | {msg['Subject']}")
        return True

    try:
        with smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(mailbox.email, mailbox.app_password)
            s.send_message(msg)
        record.status = "sent"
        db.add(record)
        log(db, "send", f"step {step_index} -> {lead.email} via {mailbox.email}")
        return True
    except Exception as e:
        record.status = "failed"
        db.add(record)
        log(db, "error", f"send failed -> {lead.email}: {e}")
        return False
