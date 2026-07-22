"""Outreach engine — FastAPI app, server-rendered UI, background scheduler."""
import os
import random
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_

from .analytics import compute as compute_analytics
from .apollo import preview_apollo, pull_apollo
from .enrich import enrich_leads
from .importer import import_csv
from .models import (Enrollment, Event, Lead, Mailbox, Message, SessionLocal,
                     Sequence, SequenceStep, Suppression, init_db, log, utcnow)
from .scheduler import poll_inboxes, process_due_sends, weekly_counter_decay
from .seed import seed_default_sequence

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    seed_default_sequence(db)
    db.close()
    scheduler.add_job(process_due_sends, "interval", minutes=5,
                      id="sends", max_instances=1)
    scheduler.add_job(poll_inboxes, "interval", minutes=10,
                      id="poll", max_instances=1)
    scheduler.add_job(weekly_counter_decay, "cron", hour=0, minute=5,
                      id="decay")
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Efforti Outreach", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "ui"))


def active_mailbox(request, db):
    """The mailbox the user is currently 'inside', from the `mb` cookie.
    None means the combined 'All mailboxes' view."""
    mid = request.cookies.get("mb")
    if mid and mid.isdigit():
        return db.query(Mailbox).get(int(mid))
    return None


def ctx(request, db, **kw):
    active_mb = kw.pop("active_mb", None) or active_mailbox(request, db)
    base = {
        "request": request,
        "dry_run": DRY_RUN,
        "mailboxes_all": db.query(Mailbox).order_by(Mailbox.id).all(),
        "active_mb": active_mb,
        "nav_counts": {
            "leads": db.query(Lead).count(),
            "active": db.query(Enrollment)
                        .filter(Enrollment.status == "active").count(),
        },
    }
    base.update(kw)
    return base


# ---------------- Mailbox context switcher ----------------
@app.post("/context/mailbox")
def switch_mailbox(request: Request, mailbox_id: str = Form("")):
    """Set (or clear) the active mailbox, then return to where you were."""
    dest = request.headers.get("referer") or "/"
    resp = RedirectResponse(dest, status_code=303)
    if mailbox_id and mailbox_id.isdigit():
        resp.set_cookie("mb", mailbox_id, max_age=31536000,
                        httponly=True, samesite="lax")
    else:
        resp.delete_cookie("mb")
    return resp


# ---------------- Dashboard ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        recent_q = db.query(Event).order_by(Event.id.desc())
        replies_q = db.query(Event).filter(Event.kind == "reply") \
                      .order_by(Event.id.desc())

        if mb:
            # Scope everything to this one mailbox's book of business.
            enr = db.query(Enrollment).filter(Enrollment.mailbox_id == mb.id)
            funnel = {
                "leads": enr.count(),
                "enrolled": enr.filter(Enrollment.status == "active").count(),
                "contacted": db.query(Message.lead_email).filter(
                    Message.mailbox_email == mb.email).distinct().count(),
                "replied": enr.filter(
                    Enrollment.status == "halted_reply").count(),
            }
            sent_total = db.query(Message).filter(
                Message.mailbox_email == mb.email,
                Message.status.in_(["sent", "dry_run"])).count()
            bounced = enr.filter(Enrollment.status == "halted_bounce").count()
            unsubbed = enr.filter(Enrollment.status == "halted_unsub").count()
            mailboxes = [mb]
            recent_q = recent_q.filter(Event.detail.contains(mb.email))
            replies_q = replies_q.filter(Event.detail.contains(mb.email))
        else:
            funnel = {
                "leads": db.query(Lead).count(),
                "enrolled": db.query(Enrollment).count(),
                "contacted": db.query(Lead).filter(
                    Lead.status.in_(["contacted", "finished", "replied"])).count(),
                "replied": db.query(Lead).filter(Lead.status == "replied").count(),
            }
            sent_total = db.query(Message).filter(
                Message.status.in_(["sent", "dry_run"])).count()
            bounced = db.query(Lead).filter(Lead.status == "bounced").count()
            unsubbed = db.query(Suppression).filter(
                Suppression.reason == "unsubscribed").count()
            mailboxes = db.query(Mailbox).all()

        recent = recent_q.limit(12).all()
        replies = replies_q.limit(5).all()
        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, db, active_mb=mb, funnel=funnel, sent_total=sent_total,
            bounced=bounced, unsubbed=unsubbed, mailboxes=mailboxes,
            recent=recent, replies=replies))
    finally:
        db.close()


# ---------------- Leads ----------------
def _leads_ctx(request, db, status="", **extra):
    """Shared context for the Leads page (used by the page and the preview)."""
    mb = active_mailbox(request, db)
    q = db.query(Lead).order_by(Lead.id.desc())
    if mb:
        # This mailbox's leads (enrolled to it) + the shared verified pool.
        enrolled_ids = [e.lead_id for e in db.query(Enrollment.lead_id)
                        .filter(Enrollment.mailbox_id == mb.id)]
        q = q.filter(or_(Lead.id.in_(enrolled_ids), Lead.status == "verified"))
    if status:
        q = q.filter(Lead.status == status)
    leads = q.limit(500).all()
    return ctx(request, db, active_mb=mb, leads=leads,
               sequences=db.query(Sequence).filter(Sequence.active.is_(True)).all(),
               status_filter=status,
               mailbox_count=db.query(Mailbox).filter(Mailbox.active.is_(True)).count(),
               verified_pool=db.query(Lead).filter(Lead.status == "verified").count(),
               **extra)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, status: str = "", pulled: int = 0,
               requested: int = 0, fetched: int = 0, has_email: int = 0,
               imported: int = 0, dupe: int = 0, no_email: int = 0,
               exhausted: int = 0):
    db = SessionLocal()
    try:
        pull_result = None
        if pulled:
            pull_result = {"requested": requested, "fetched": fetched,
                           "has_email": has_email, "imported": imported,
                           "dupe": dupe, "no_email": no_email,
                           "exhausted": bool(exhausted)}
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, status=status, pull_result=pull_result))
    finally:
        db.close()


@app.post("/leads/apollo_preview", response_class=HTMLResponse)
def apollo_preview(request: Request, count: int = Form(25),
                   keywords: str = Form(""), locations: str = Form("")):
    """Free search-only preview: show who Apollo has before spending credits."""
    db = SessionLocal()
    try:
        count = max(1, min(200, count))
        kw = [k.strip() for k in keywords.split(",") if k.strip()] or None
        loc = [l.strip() for l in locations.split(",") if l.strip()] or None
        preview = preview_apollo(keywords=kw, locations=loc,
                                 pages=max(1, (count + 99) // 100),
                                 per_page=min(count, 100))
        pf = {"keywords": keywords, "locations": locations, "count": count}
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, preview=preview, preview_filters=pf))
    finally:
        db.close()


@app.post("/leads/import", response_class=HTMLResponse)
async def leads_import(request: Request, file: UploadFile = File(...),
                       mx_check: str = Form("on")):
    db = SessionLocal()
    try:
        content = await file.read()
        result = import_csv(db, content, do_mx=(mx_check == "on"))
        result["filename"] = file.filename
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, import_result=result))
    finally:
        db.close()


@app.post("/leads/apollo_pull")
def apollo_pull(count: int = Form(25), keywords: str = Form(""),
                locations: str = Form("")):
    """Pull `count` new C-suite leads from Apollo (default ICP). Keeps paging
    until it hits the target; runs the same verify/dedupe/suppression gates."""
    db = SessionLocal()
    try:
        count = max(1, min(200, count))
        kw = [k.strip() for k in keywords.split(",") if k.strip()] or None
        loc = [l.strip() for l in locations.split(",") if l.strip()] or None
        s = pull_apollo(db, keywords=kw, locations=loc, target=count)
        dupe = s["skipped_duplicate"] + s["skipped_domain_dupe"]
        return RedirectResponse(
            f"/leads?pulled=1&requested={count}&fetched={s['fetched']}"
            f"&has_email={s['has_email']}&imported={s['imported']}&dupe={dupe}"
            f"&no_email={s['no_email']}&exhausted={1 if s['exhausted'] else 0}",
            status_code=303)
    finally:
        db.close()


@app.post("/leads/enrich")
def enrich(limit: int = Form(500)):
    """Generate an AI-written personalized opener for each verified lead."""
    db = SessionLocal()
    try:
        enrich_leads(db, limit=limit)
        return RedirectResponse("/leads", status_code=303)
    finally:
        db.close()


@app.post("/leads/enroll")
def enroll(request: Request, sequence_id: int = Form(...)):
    """Enroll all 'verified' leads into a sequence. If a mailbox is active,
    all leads go to that one mailbox; otherwise they spread round-robin across
    every active mailbox. Staggered so day-one volume respects warm-up caps."""
    db = SessionLocal()
    try:
        seq = db.query(Sequence).get(sequence_id)
        mb = active_mailbox(request, db)
        if mb and mb.active:
            mailboxes = [mb]
        else:
            mailboxes = db.query(Mailbox).filter(Mailbox.active.is_(True)).all()
        if not seq or not mailboxes:
            return RedirectResponse("/leads?err=no_seq_or_mailbox",
                                    status_code=303)
        leads = db.query(Lead).filter(Lead.status == "verified").all()
        now = utcnow()
        per_day_capacity = sum(m.effective_cap() for m in mailboxes)
        for i, lead in enumerate(leads):
            mb = mailboxes[i % len(mailboxes)]
            day_offset = i // max(1, per_day_capacity)
            db.add(Enrollment(
                lead_id=lead.id, sequence_id=seq.id, mailbox_id=mb.id,
                next_send_at=now + timedelta(days=day_offset,
                                             minutes=random.randint(0, 120)),
            ))
            lead.status = "enrolled"
        log(db, "enroll", f"Enrolled {len(leads)} leads into '{seq.name}' "
                          f"across {len(mailboxes)} mailboxes "
                          f"(~{per_day_capacity}/day capacity)")
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()


@app.post("/leads/{lead_id}/suppress")
def suppress_lead(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).get(lead_id)
        if lead:
            db.merge(Suppression(email=lead.email, reason="manual"))
            lead.status = "unsubscribed"
            for enr in db.query(Enrollment).filter(
                    Enrollment.lead_id == lead.id,
                    Enrollment.status == "active").all():
                enr.status = "halted_manual"
            log(db, "unsub", f"{lead.email} manually suppressed")
            db.commit()
        return RedirectResponse("/leads", status_code=303)
    finally:
        db.close()


# ---------------- Analytics ----------------
@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        q = db.query(Lead).order_by(Lead.id.desc())
        if mb:
            enrolled_ids = [e.lead_id for e in db.query(Enrollment.lead_id)
                            .filter(Enrollment.mailbox_id == mb.id)]
            q = q.filter(or_(Lead.id.in_(enrolled_ids),
                             Lead.status == "verified"))
        leads = q.limit(300).all()
        a = compute_analytics(db, mailbox=mb, leads=leads)
        return templates.TemplateResponse(request, "analytics.html", ctx(
            request, db, active_mb=mb, a=a))
    finally:
        db.close()


# ---------------- Sequences ----------------
@app.get("/sequences", response_class=HTMLResponse)
def sequences_page(request: Request):
    db = SessionLocal()
    try:
        seqs = db.query(Sequence).all()
        # Current follow-up cadence = wait_days on the first follow-up step
        cadence = 3
        first = db.query(Sequence).first()
        if first:
            fus = [s for s in first.steps if s.step_index > 0]
            if fus:
                cadence = fus[0].wait_days
        return templates.TemplateResponse(request, "sequences.html",
                                          ctx(request, db, sequences=seqs,
                                              cadence=cadence))
    finally:
        db.close()


@app.post("/sequences/followup_gap")
def set_followup_gap(days: int = Form(...)):
    """Set the gap (in days) between every follow-up touch across all sequences."""
    db = SessionLocal()
    try:
        days = max(1, min(30, days))
        for step in db.query(SequenceStep).filter(SequenceStep.step_index > 0).all():
            step.wait_days = days
        log(db, "sequence", f"Follow-up cadence set to every {days} days")
        db.commit()
        return RedirectResponse("/sequences", status_code=303)
    finally:
        db.close()


@app.post("/sequences/step/{step_id}")
def update_step(step_id: int, subject: str = Form(""), body: str = Form(...),
                wait_days: int = Form(0)):
    db = SessionLocal()
    try:
        step = db.query(SequenceStep).get(step_id)
        if step:
            step.subject = subject
            step.body = body
            step.wait_days = wait_days
            db.commit()
        return RedirectResponse("/sequences", status_code=303)
    finally:
        db.close()


# ---------------- Mailboxes ----------------
@app.get("/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: Request):
    db = SessionLocal()
    try:
        boxes = db.query(Mailbox).all()
        return templates.TemplateResponse(request, "mailboxes.html",
                                          ctx(request, db, mailboxes=boxes))
    finally:
        db.close()


@app.post("/mailboxes/add")
def add_mailbox(email: str = Form(...), display_name: str = Form(""),
                app_password: str = Form(...), daily_cap: int = Form(25)):
    db = SessionLocal()
    try:
        db.add(Mailbox(email=email.strip().lower(),
                       display_name=display_name.strip(),
                       app_password=app_password.strip(),
                       daily_cap=daily_cap))
        log(db, "mailbox", f"Added mailbox {email}")
        db.commit()
        return RedirectResponse("/mailboxes", status_code=303)
    finally:
        db.close()


@app.post("/mailboxes/{mailbox_id}/toggle")
def toggle_mailbox(mailbox_id: int):
    db = SessionLocal()
    try:
        mb = db.query(Mailbox).get(mailbox_id)
        if mb:
            mb.active = not mb.active
            if mb.active:
                mb.paused_reason = ""
            db.commit()
        return RedirectResponse("/mailboxes", status_code=303)
    finally:
        db.close()


# ---------------- Activity ----------------
@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, kind: str = ""):
    db = SessionLocal()
    try:
        q = db.query(Event).order_by(Event.id.desc())
        if kind:
            q = q.filter(Event.kind == kind)
        events = q.limit(300).all()
        return templates.TemplateResponse(request, "activity.html", ctx(
            request, db, events=events, kind_filter=kind))
    finally:
        db.close()


@app.post("/activity/clear")
def clear_activity():
    """Wipe the activity log for a clean, live feed (logs only — leads,
    mailboxes, sends and suppressions are untouched)."""
    db = SessionLocal()
    try:
        db.query(Event).delete()
        db.commit()
        return RedirectResponse("/activity", status_code=303)
    finally:
        db.close()


# ---------------- Unsubscribe (public) ----------------
@app.get("/u/{token}", response_class=HTMLResponse)
@app.post("/u/{token}", response_class=HTMLResponse)  # RFC 8058 one-click
def unsubscribe(token: str):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.unsub_token == token).first()
        if lead:
            db.merge(Suppression(email=lead.email, reason="unsubscribed"))
            lead.status = "unsubscribed"
            for enr in db.query(Enrollment).filter(
                    Enrollment.lead_id == lead.id,
                    Enrollment.status == "active").all():
                enr.status = "halted_unsub"
            log(db, "unsub", f"{lead.email} unsubscribed via link")
            db.commit()
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:48px;"
            "color:#374151'><h3>You're unsubscribed.</h3>"
            "<p>You won't hear from us again.</p></body></html>")
    finally:
        db.close()


# ---------------- Manual triggers (useful in dry-run testing) ----------------
@app.post("/run/sends")
def trigger_sends():
    process_due_sends()
    return RedirectResponse("/activity", status_code=303)
