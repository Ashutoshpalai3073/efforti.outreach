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
from .apollo import SIZE_PRESETS, preview_apollo, pull_apollo
from .enrich import enrich_leads
from .importer import import_csv
from .models import (Enrollment, Event, Lead, Mailbox, Message, SessionLocal,
                     Sequence, SequenceStep, Suppression, init_db, log, utcnow)
from .scheduler import (due_counts, poll_inboxes, poll_now, process_due_sends,
                        send_due_now, send_enrollment_step, weekly_counter_decay)
from .seed import seed_default_sequence

# On sleep-prone hosts (e.g. Render free tier) the background sender can't be
# trusted to fire on time. MANUAL_SEND_ONLY (default ON) turns off automatic
# sending/polling entirely — you drive it from the dashboard buttons, so the
# server being asleep never causes a missed or mistimed send.
MANUAL_SEND_ONLY = os.environ.get("MANUAL_SEND_ONLY", "true").lower() == "true"
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    seed_default_sequence(db)
    db.close()
    if not MANUAL_SEND_ONLY:
        # Always-on host: let the scheduler auto-send and auto-poll.
        scheduler.add_job(process_due_sends, "interval", minutes=5,
                          id="sends", max_instances=1)
        scheduler.add_job(poll_inboxes, "interval", minutes=10,
                          id="poll", max_instances=1)
    # Counter decay is cheap and safe to keep in both modes.
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
def dashboard(request: Request, sent: int = -1, capped: int = 0,
              suppressed: int = 0, nomb: int = 0, failed: int = 0,
              polled: int = 0, pollskip: int = 0):
    db = SessionLocal()
    try:
        mb = active_mailbox(request, db)
        send_result = None
        if sent >= 0:
            send_result = {"sent": sent, "capped": capped,
                           "suppressed": suppressed, "no_mailbox": nomb,
                           "failed": failed}
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
                Message.status == "sent").count()
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
                Message.status == "sent").count()
            bounced = db.query(Lead).filter(Lead.status == "bounced").count()
            unsubbed = db.query(Suppression).filter(
                Suppression.reason == "unsubscribed").count()
            mailboxes = db.query(Mailbox).all()

        recent = recent_q.limit(12).all()
        replies = replies_q.limit(5).all()
        return templates.TemplateResponse(request, "dashboard.html", ctx(
            request, db, active_mb=mb, funnel=funnel, sent_total=sent_total,
            bounced=bounced, unsubbed=unsubbed, mailboxes=mailboxes,
            recent=recent, replies=replies, due=due_counts(db),
            manual_mode=MANUAL_SEND_ONLY,
            send_result=send_result, polled=polled, pollskip=pollskip))
    finally:
        db.close()


# ---------------- Leads ----------------
def _step_label(i, short=False):
    """Human labels for a sequence step. Step 0 is the first email; the rest
    are follow-ups. No jargon — 'First email', 'Follow-up 1', 'Follow-up 2'."""
    if i == 0:
        return "First" if short else "First email"
    return f"F{i}" if short else f"Follow-up {i}"


def _lead_progress(db, leads):
    """Per-lead outreach state for the action column: which steps already went
    out, which step is next to send, and whether the lead has stopped/finished.
    Returns (progress_by_lead_id, total_steps)."""
    if not leads:
        return {}, 0
    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    total = len(seq.steps) if seq and seq.steps else 3
    ids = [l.id for l in leads]
    emails = [l.email for l in leads]
    enr_by_lead = {}
    for e in (db.query(Enrollment)
              .filter(Enrollment.lead_id.in_(ids),
                      Enrollment.status == "active")
              .order_by(Enrollment.id).all()):
        enr_by_lead[e.lead_id] = e            # keep the latest active one
    sent = {}
    for m in (db.query(Message.lead_email, Message.step_index)
              .filter(Message.lead_email.in_(emails),
                      Message.status == "sent").all()):
        sent.setdefault(m.lead_email, set()).add(m.step_index)
    prog = {}
    for l in leads:
        done_steps = sorted(sent.get(l.email, set()))
        if l.status in ("replied", "bounced", "unsubscribed"):
            state, nxt = l.status, None
        else:
            enr = enr_by_lead.get(l.id)
            cur = enr.current_step if enr else 0
            if cur >= total:
                state, nxt = "done", None
            else:
                state, nxt = "ready", cur
        prog[l.id] = {
            "sent": done_steps, "next": nxt, "state": state,
            "action": None if nxt is None else
            ("Send first email" if nxt == 0 else f"Send follow-up {nxt}"),
        }
    return prog, total


def _pick_mailbox(request, db):
    """The mailbox to send from: the active one if set, else the first active
    mailbox. None if there are no active mailboxes."""
    mb = active_mailbox(request, db)
    if mb and mb.active:
        return mb
    return (db.query(Mailbox).filter(Mailbox.active.is_(True))
            .order_by(Mailbox.id).first())


def _chosen_mailbox(db, mailbox_id):
    """The specific active mailbox the user picked in the 'Send from' dropdown,
    or None when they left it on 'spread across all' (or picked an invalid one)."""
    if mailbox_id and str(mailbox_id).isdigit():
        mb = db.query(Mailbox).get(int(mailbox_id))
        if mb and mb.active:
            return mb
    return None


def _ensure_enrollment(db, lead, mailbox):
    """Get this lead's active enrollment, creating one (auto-enroll into the
    default sequence) the first time you send to a not-yet-enrolled lead — so
    the user never has to run a separate 'enroll' step."""
    enr = (db.query(Enrollment)
           .filter(Enrollment.lead_id == lead.id,
                   Enrollment.status == "active")
           .order_by(Enrollment.id.desc()).first())
    if enr:
        return enr
    seq = db.query(Sequence).filter(Sequence.active.is_(True)).first()
    if not seq:
        return None
    enr = Enrollment(lead_id=lead.id, sequence_id=seq.id,
                     mailbox_id=mailbox.id, current_step=0,
                     next_send_at=utcnow())
    db.add(enr)
    db.flush()
    if lead.status == "verified":
        lead.status = "enrolled"
    return enr


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
    prog, total = _lead_progress(db, leads)
    return ctx(request, db, active_mb=mb, leads=leads,
               progress=prog, total_steps=total,
               step_labels=[_step_label(i, short=True) for i in range(total)],
               sequences=db.query(Sequence).filter(Sequence.active.is_(True)).all(),
               status_filter=status,
               mailbox_count=db.query(Mailbox).filter(Mailbox.active.is_(True)).count(),
               verified_pool=db.query(Lead).filter(Lead.status == "verified").count(),
               **extra)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, status: str = "", pulled: int = 0,
               brands: int = 0, per_brand: int = 0, brands_filled: int = 0,
               fetched: int = 0, imported: int = 0, brand_full: int = 0,
               dupe: int = 0, no_email: int = 0, exhausted: int = 0,
               one: str = "", bulk: int = 0, bsent: int = 0, bcapped: int = 0,
               bskip: int = 0, bnomb: int = 0, bstep: int = -1):
    db = SessionLocal()
    try:
        pull_result = None
        if pulled:
            pull_result = {"brands": brands, "per_brand": per_brand,
                           "brands_filled": brands_filled, "fetched": fetched,
                           "imported": imported, "brand_full": brand_full,
                           "dupe": dupe, "no_email": no_email,
                           "target_total": brands * per_brand,
                           "exhausted": bool(exhausted)}
        send_feedback = None
        if one:
            send_feedback = {"kind": "one", "status": one}
        elif bulk:
            send_feedback = {"kind": "bulk", "sent": bsent, "capped": bcapped,
                             "skipped": bskip, "no_mailbox": bnomb,
                             "label": _step_label(bstep) if bstep >= 0 else ""}
        return templates.TemplateResponse(request, "leads.html", _leads_ctx(
            request, db, status=status, pull_result=pull_result,
            send_feedback=send_feedback))
    finally:
        db.close()


def _apollo_filters(keywords, locations, size_range):
    """Shared parsing for the Apollo form fields."""
    kw = [k.strip() for k in keywords.split(",") if k.strip()] or None
    loc = [l.strip() for l in locations.split(",") if l.strip()] or None
    sizes = SIZE_PRESETS.get(size_range) or SIZE_PRESETS["startup"]
    return kw, loc, sizes


@app.post("/leads/apollo_preview", response_class=HTMLResponse)
def apollo_preview(request: Request, brands: int = Form(20),
                   per_brand: int = Form(5), keywords: str = Form(""),
                   locations: str = Form(""), size_range: str = Form("startup")):
    """Free search-only preview: show who Apollo has, grouped by brand, before
    spending any credits."""
    db = SessionLocal()
    try:
        brands = max(1, min(100, brands))
        per_brand = max(1, min(10, per_brand))
        kw, loc, sizes = _apollo_filters(keywords, locations, size_range)
        preview = preview_apollo(keywords=kw, locations=loc, size_ranges=sizes,
                                 brands=brands, per_brand=per_brand)
        pf = {"keywords": keywords, "locations": locations,
              "size_range": size_range, "brands": brands,
              "per_brand": per_brand}
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
def apollo_pull(brands: int = Form(20), per_brand: int = Form(5),
                keywords: str = Form(""), locations: str = Form(""),
                size_range: str = Form("startup")):
    """Pull the top `per_brand` execs at up to `brands` companies from Apollo
    (default ICP). Enforces the per-brand cap + brand scope, and runs the same
    verify/dedupe/suppression gates."""
    db = SessionLocal()
    try:
        brands = max(1, min(100, brands))
        per_brand = max(1, min(10, per_brand))
        kw, loc, sizes = _apollo_filters(keywords, locations, size_range)
        s = pull_apollo(db, keywords=kw, locations=loc, size_ranges=sizes,
                        brands=brands, per_brand=per_brand)
        return RedirectResponse(
            f"/leads?pulled=1&brands={s['brands']}&per_brand={s['per_brand']}"
            f"&brands_filled={s['brands_filled']}&fetched={s['fetched']}"
            f"&imported={s['imported']}&brand_full={s['skipped_brand_full']}"
            f"&dupe={s['skipped_duplicate']}&no_email={s['no_email']}"
            f"&exhausted={1 if s['exhausted'] else 0}",
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


@app.post("/leads/{lead_id}/send")
def send_one_lead(request: Request, lead_id: int, step: int = Form(...),
                  mailbox_id: str = Form("")):
    """Send one specific email (first email, or a chosen follow-up) to a single
    lead, right now, from the chosen mailbox. Auto-enrolls on the first send.
    (Follow-ups always go from the mailbox that started the thread.)"""
    db = SessionLocal()
    try:
        lead = db.query(Lead).get(lead_id)
        mb = _chosen_mailbox(db, mailbox_id) or _pick_mailbox(request, db)
        if not lead:
            return RedirectResponse("/leads", status_code=303)
        if not mb:
            return RedirectResponse("/leads?one=no_mailbox", status_code=303)
        enr = _ensure_enrollment(db, lead, mb)
        if not enr:
            return RedirectResponse("/leads?one=no_sequence", status_code=303)
        result = send_enrollment_step(db, enr, step)
        db.commit()
        return RedirectResponse(f"/leads?one={result}", status_code=303)
    finally:
        db.close()


@app.post("/leads/send_selected")
def send_selected(request: Request, step: int = Form(...), ids: str = Form(""),
                  mailbox_id: str = Form("")):
    """Send ONE step to every selected lead that's ready for it, from the chosen
    mailbox. Because the step is fixed, everyone in a click gets the same email —
    first emails are never mixed with follow-ups. If 'spread across all' is
    chosen, new leads are round-robined across active mailboxes. Already-enrolled
    leads keep sending from their own thread's mailbox."""
    db = SessionLocal()
    try:
        lead_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        chosen = _chosen_mailbox(db, mailbox_id)
        actives = (db.query(Mailbox).filter(Mailbox.active.is_(True))
                   .order_by(Mailbox.id).all())
        c = {"sent": 0, "capped": 0, "skipped": 0, "no_mailbox": 0}
        if not chosen and not actives:
            return RedirectResponse(
                f"/leads?bulk=1&bstep={step}&bnomb={len(lead_ids)}",
                status_code=303)
        rr = 0                                  # round-robin cursor for new leads
        for lid in lead_ids:
            lead = db.query(Lead).get(lid)
            if not lead:
                continue
            existing = (db.query(Enrollment)
                        .filter(Enrollment.lead_id == lid,
                                Enrollment.status == "active").first())
            if existing:
                enr = existing                  # follow-up → keep its mailbox
            else:
                mb = chosen or actives[rr % len(actives)]
                rr += 1
                enr = _ensure_enrollment(db, lead, mb)
            if not enr:
                c["skipped"] += 1
                continue
            r = send_enrollment_step(db, enr, step)
            if r == "sent":
                c["sent"] += 1
            elif r == "capped":
                c["capped"] += 1
            elif r == "no_mailbox":
                c["no_mailbox"] += 1
            else:                               # out_of_order / stopped / done
                c["skipped"] += 1
        db.commit()
        return RedirectResponse(
            f"/leads?bulk=1&bstep={step}&bsent={c['sent']}&bcapped={c['capped']}"
            f"&bskip={c['skipped']}&bnomb={c['no_mailbox']}", status_code=303)
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


# ---------------- Manual triggers (the primary way to send) ----------------
@app.post("/run/sends")
def trigger_sends():
    """Send every email that's due right now — first touches and follow-ups —
    in one batch. Caps and suppression still apply. Returns to the dashboard
    with a summary of what went out."""
    s = send_due_now()
    return RedirectResponse(
        f"/?sent={s['sent']}&capped={s['capped']}&suppressed={s['suppressed']}"
        f"&nomb={s['no_mailbox']}&failed={s['failed']}",
        status_code=303)


@app.post("/run/poll")
def trigger_poll():
    """Manually check every mailbox for replies and bounces (live mode only)."""
    r = poll_now()
    flag = "polled" if r.get("polled") else "pollskip"
    return RedirectResponse(f"/?{flag}=1", status_code=303)
