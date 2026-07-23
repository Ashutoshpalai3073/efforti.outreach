"""SMTP sending with proper threading and unsubscribe headers."""
import base64
import html as htmllib
import imaplib
import os
import smtplib
import uuid
from email.message import EmailMessage
from email.utils import formatdate

from jinja2 import Template

from .models import Enrollment, Lead, Mailbox, Message, log, utcnow

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")


def render(template_str: str, lead: Lead) -> str:
    return Template(template_str).render(
        first_name=lead.first_name or "there",
        last_name=lead.last_name,
        title=lead.title,
        company=lead.company or "your company",
        trigger=lead.trigger,
        industry=getattr(lead, "industry", ""),
        opener=getattr(lead, "opener", "") or "",
        research=getattr(lead, "company_research", "") or "",
    )


def verify_credentials(email: str, app_password: str,
                       smtp_host: str = "smtp.gmail.com", smtp_port: int = 587,
                       imap_host: str = "imap.gmail.com") -> tuple:
    """Prove a mailbox can actually log in BEFORE we save it, so typo'd emails
    or wrong app passwords are rejected instead of silently failing at send
    time. Returns (ok, reason):
      ok=True                 -> credentials work, safe to add
      (False, 'bad_auth')     -> email / app password rejected by Google
      (False, 'connect:...')  -> couldn't reach the mail server (network/host)

    Checks SMTP (sending) first — that's the credential that must work — then
    does a best-effort IMAP login (reply detection). A flaky IMAP probe never
    blocks a mailbox whose SMTP login already succeeded.
    """
    if not email or not app_password:
        return False, "missing"
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(email, app_password)
    except smtplib.SMTPAuthenticationError:
        return False, "bad_auth"
    except Exception as e:                      # DNS / timeout / refused / etc.
        return False, f"connect:{e}"
    try:
        imap = imaplib.IMAP4_SSL(imap_host, timeout=15)
        imap.login(email, app_password)
        imap.logout()
    except imaplib.IMAP4.error:
        return False, "bad_auth_imap"
    except Exception:
        pass                                    # SMTP already proved the login
    return True, ""


LOGO_CID = "efsig-logo"      # Content-ID that the signature <img> references


def _sig_inner_html(mailbox: Mailbox, logo_src: str) -> str:
    """The branded signature block as an email-safe (table-based) HTML string.
    `logo_src` is 'cid:...' for a real send or a data: URI for the on-page
    preview — the layout is otherwise identical, so what you preview is what
    the client receives."""
    e = htmllib.escape
    name = (mailbox.display_name or "").strip()
    title = (mailbox.sig_title or "").strip()
    company = (mailbox.sig_company or "").strip()
    phone = (mailbox.sig_phone or "").strip()
    email = (mailbox.sig_email or "").strip() or mailbox.email

    logo_cell = ""
    if logo_src:
        logo_cell = (
            '<td style="vertical-align:top;padding-right:18px">'
            f'<img src="{e(logo_src)}" alt="{e(company or name or "logo")}" '
            'style="display:block;border:0;max-height:60px;height:auto">'
            '</td>')
    rows = []
    for val in (name, title, company):
        if val:
            rows.append(f'<div style="font-weight:bold">{e(val)}</div>')
    if phone:
        rows.append(f'<div style="font-weight:bold">Contact No: {e(phone)}</div>')
    rows.append(
        '<div style="font-weight:bold">Email Id: '
        f'<a href="mailto:{e(email)}" style="color:#1a73e8;text-decoration:none">'
        f'{e(email)}</a></div>')
    text_cell = (
        '<td style="vertical-align:top;font-family:Arial,Helvetica,sans-serif;'
        'font-size:13px;color:#000000;line-height:1.55">' + "".join(rows) + '</td>')
    return ('<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="margin-top:22px;border-collapse:collapse"><tr>'
            + logo_cell + text_cell + '</tr></table>')


def signature_html(mailbox: Mailbox):
    """(html, logo) for the sent email. `logo` is (bytes, subtype) to attach
    inline, or None. Returns ('', None) when the mailbox has signatures off."""
    if not getattr(mailbox, "signature_on", True):
        return "", None
    logo = None
    logo_src = ""
    if getattr(mailbox, "logo_b64", ""):
        subtype = (mailbox.logo_mime or "image/png").split("/")[-1] or "png"
        logo = (base64.b64decode(mailbox.logo_b64), subtype)
        logo_src = f"cid:{LOGO_CID}"
    return _sig_inner_html(mailbox, logo_src), logo


def signature_preview_html(mailbox: Mailbox) -> str:
    """Same block rendered for the browser, with the logo as a data: URI so it
    shows without an email client. Always rendered (ignores signature_on) so the
    editor preview is visible even while the signature is toggled off."""
    logo_src = ""
    if getattr(mailbox, "logo_b64", ""):
        logo_src = f"data:{mailbox.logo_mime or 'image/png'};base64,{mailbox.logo_b64}"
    return _sig_inner_html(mailbox, logo_src)


def signature_text(mailbox: Mailbox) -> str:
    """Plain-text version of the signature for the text/plain alternative."""
    if not getattr(mailbox, "signature_on", True):
        return ""
    lines = [v for v in (
        (mailbox.display_name or "").strip(),
        (mailbox.sig_title or "").strip(),
        (mailbox.sig_company or "").strip(),
    ) if v]
    phone = (mailbox.sig_phone or "").strip()
    if phone:
        lines.append(f"Contact No: {phone}")
    email = (mailbox.sig_email or "").strip() or mailbox.email
    lines.append(f"Email Id: {email}")
    return "\n".join(lines)


def make_message_id(mailbox: Mailbox) -> str:
    domain = mailbox.email.split("@", 1)[1]
    return f"<{uuid.uuid4().hex}@{domain}>"


def build_email(mailbox: Mailbox, lead: Lead, enrollment: Enrollment,
                subject: str, body: str, cc: list = None,
                bcc: list = None) -> EmailMessage:
    msg = EmailMessage()
    msg_id = make_message_id(mailbox)
    msg["Message-ID"] = msg_id
    msg["Date"] = formatdate(localtime=False)
    msg["From"] = f"{mailbox.display_name} <{mailbox.email}>" \
        if mailbox.display_name else mailbox.email
    msg["To"] = lead.email
    # CC is visible to the recipient; BCC is stripped from the sent message by
    # smtplib.send_message but still added to the envelope, so those addresses
    # get a copy without the lead seeing them.
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

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

    sig_text = signature_text(mailbox)
    sig_html, logo = signature_html(mailbox)

    # text/plain (fallback for every client + better deliverability)
    text_footer = (f"\n\n--\nIf you'd rather not hear from me, one click opts "
                   f"you out: {unsub_url}")
    text_body = body + (f"\n\n{sig_text}" if sig_text else "") + text_footer
    msg.set_content(text_body)

    # text/html (carries the branded signature + inline logo)
    body_html = htmllib.escape(body).replace("\n", "<br>")
    unsub_html = (
        '<div style="color:#8a8a8a;font-size:12px;margin-top:26px">'
        f'If you\'d rather not hear from me, <a href="{htmllib.escape(unsub_url)}" '
        'style="color:#8a8a8a">one click opts you out</a>.</div>')
    html_doc = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        f'color:#000000;line-height:1.55">{body_html}{sig_html}{unsub_html}</div>')
    msg.add_alternative(html_doc, subtype="html")

    # Inline logo: attach it inside the HTML part (multipart/related) so the
    # signature <img src="cid:..."> resolves. Only touches the HTML alternative;
    # the plain-text part stays clean.
    if logo:
        logo_bytes, subtype = logo
        html_part = msg.get_payload()[1]
        html_part.add_related(logo_bytes, maintype="image", subtype=subtype,
                              cid=f"<{LOGO_CID}>")
    return msg, msg_id


def send(db, mailbox: Mailbox, lead: Lead, enrollment: Enrollment,
         subject_tpl: str, body_tpl: str, step_index: int,
         cc: list = None, bcc: list = None) -> bool:
    subject = render(subject_tpl, lead) if subject_tpl else ""
    body = render(body_tpl, lead)
    msg, msg_id = build_email(mailbox, lead, enrollment, subject, body,
                              cc=cc, bcc=bcc)

    record = Message(
        enrollment_id=enrollment.id, lead_email=lead.email,
        mailbox_email=mailbox.email, step_index=step_index,
        subject=msg["Subject"], body=body, message_id=msg_id,
    )

    try:
        with smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(mailbox.email, mailbox.app_password)
            s.send_message(msg)      # envelope includes Cc + Bcc; Bcc header stripped
        record.status = "sent"
        db.add(record)
        extra = ""
        if cc:
            extra += f" cc:{','.join(cc)}"
        if bcc:
            extra += f" bcc:{','.join(bcc)}"
        log(db, "send", f"step {step_index} -> {lead.email} via {mailbox.email}"
                        f"{extra}")
        return True
    except Exception as e:
        record.status = "failed"
        db.add(record)
        log(db, "error", f"send failed -> {lead.email}: {e}")
        return False
