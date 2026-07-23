"""Database models for the outreach engine."""
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Mailbox(Base):
    """A sending mailbox (Gmail/Workspace account on a lookalike domain)."""
    __tablename__ = "mailboxes"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, default="")
    smtp_host = Column(String, default="smtp.gmail.com")
    smtp_port = Column(Integer, default=587)
    imap_host = Column(String, default="imap.gmail.com")
    app_password = Column(String, nullable=False)  # Gmail app password
    daily_cap = Column(Integer, default=25)        # hard ceiling per day
    warmup_start = Column(Integer, default=8)      # day-1 volume
    warmup_step = Column(Integer, default=2)       # +N per day until cap
    created_at = Column(DateTime, default=utcnow)
    active = Column(Boolean, default=True)
    paused_reason = Column(String, default="")     # set on auto-pause
    sent_today = Column(Integer, default=0)
    sent_today_date = Column(String, default="")   # YYYY-MM-DD
    bounces_7d = Column(Integer, default=0)
    sends_7d = Column(Integer, default=0)
    # Signature — Gmail's built-in signature is NOT applied when we send over
    # SMTP, so we build & append our own branded block per mailbox.
    signature_on = Column(Boolean, default=True)
    sig_title = Column(String, default="")         # e.g. "Founder's office"
    sig_company = Column(String, default="")       # e.g. "Efforti.ai"
    sig_phone = Column(String, default="")         # e.g. "+91 9348153073"
    sig_email = Column(String, default="")         # "Email Id" shown; blank = mailbox email
    logo_b64 = Column(Text, default="")            # inline logo, base64
    logo_mime = Column(String, default="")         # e.g. "image/png"

    def sig_contact_email(self) -> str:
        return (self.sig_email or "").strip() or self.email

    def effective_cap(self) -> int:
        """Warm-up ramp: start low, add warmup_step per day of age, cap at daily_cap."""
        age_days = max(0, (utcnow() - self.created_at).days)
        return min(self.daily_cap, self.warmup_start + age_days * self.warmup_step)

    def bounce_rate(self) -> float:
        return (self.bounces_7d / self.sends_7d) if self.sends_7d else 0.0


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    title = Column(String, default="")
    company = Column(String, default="")
    company_domain = Column(String, default="", index=True)
    company_size = Column(String, default="")
    source = Column(String, default="csv")         # apollo / crunchbase / angellist...
    trigger = Column(String, default="")           # e.g. "raised seed Mar 2026"
    industry = Column(String, default="")          # from Apollo, feeds personalization
    company_desc = Column(Text, default="")        # short blurb, feeds AI opener
    company_research = Column(Text, default="")    # live web research, per company, feeds opener
    researched_at = Column(DateTime)               # when company_research was last refreshed
    opener = Column(Text, default="")              # AI-written first line, per lead
    timezone_offset = Column(Float, default=5.5)   # hours vs UTC; IST default
    status = Column(String, default="new", index=True)
    # new -> verified -> enrolled -> contacted -> replied | bounced | unsubscribed | finished
    verify_result = Column(String, default="")     # ok / no_mx / bad_syntax / risky
    unsub_token = Column(String, default=lambda: secrets.token_urlsafe(16), unique=True)
    created_at = Column(DateTime, default=utcnow)
    enrollments = relationship("Enrollment", back_populates="lead")


class Sequence(Base):
    __tablename__ = "sequences"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    steps = relationship("SequenceStep", back_populates="sequence",
                         order_by="SequenceStep.step_index")


class SequenceStep(Base):
    __tablename__ = "sequence_steps"
    id = Column(Integer, primary_key=True)
    sequence_id = Column(Integer, ForeignKey("sequences.id"))
    step_index = Column(Integer, nullable=False)   # 0, 1, 2...
    wait_days = Column(Integer, default=0)         # days after previous step
    subject = Column(String, default="")           # empty on follow-ups = same thread
    body = Column(Text, nullable=False)            # Jinja2: {{first_name}} etc.
    sequence = relationship("Sequence", back_populates="steps")


class Enrollment(Base):
    """A lead progressing through a sequence."""
    __tablename__ = "enrollments"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True)
    sequence_id = Column(Integer, ForeignKey("sequences.id"))
    mailbox_id = Column(Integer, ForeignKey("mailboxes.id"))
    current_step = Column(Integer, default=0)
    next_send_at = Column(DateTime, index=True)
    status = Column(String, default="active", index=True)
    # active -> finished | halted_reply | halted_bounce | halted_unsub | halted_manual
    thread_message_id = Column(String, default="")  # first Message-ID for threading
    thread_subject = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    lead = relationship("Lead", back_populates="enrollments")
    mailbox = relationship("Mailbox")
    sequence = relationship("Sequence")


class Message(Base):
    """Every send attempt."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), index=True)
    lead_email = Column(String, index=True)
    mailbox_email = Column(String)
    step_index = Column(Integer)
    subject = Column(String)
    body = Column(Text)
    message_id = Column(String, index=True)        # RFC Message-ID we generated
    sent_at = Column(DateTime, default=utcnow)
    status = Column(String, default="sent")        # sent / failed


class Suppression(Base):
    """Never contact these again. Checked at import, enroll, and send time."""
    __tablename__ = "suppressions"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    reason = Column(String)                        # unsubscribed / bounced / manual / pipeline
    created_at = Column(DateTime, default=utcnow)


class Event(Base):
    """Audit log shown in the Activity view."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    kind = Column(String, index=True)  # send/reply/bounce/unsub/pause/import/enroll/error
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow)


# DB location is configurable so it can live on a mounted volume in production.
# Local default: ./outreach.db.  On a host with a persistent disk at /data:
#   DATABASE_URL=sqlite:////data/outreach.db   (note the 4 slashes = absolute)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///outreach.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_sqlite()


def _migrate_sqlite():
    """create_all() never ALTERs an existing table, so on an already-created
    SQLite DB newly-added columns would be missing. Add any that aren't there
    yet — a no-op once the DB is up to date."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    adds_by_table = {
        "mailboxes": {
            "signature_on": "BOOLEAN DEFAULT 1",
            "sig_title": "VARCHAR DEFAULT ''",
            "sig_company": "VARCHAR DEFAULT ''",
            "sig_phone": "VARCHAR DEFAULT ''",
            "sig_email": "VARCHAR DEFAULT ''",
            "logo_b64": "TEXT DEFAULT ''",
            "logo_mime": "VARCHAR DEFAULT ''",
        },
        "leads": {
            "company_research": "TEXT DEFAULT ''",
            "researched_at": "DATETIME",
        },
    }
    with engine.begin() as conn:
        for table, adds in adds_by_table.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in adds.items():
                if name not in have:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def log(db, kind: str, detail: str):
    db.add(Event(kind=kind, detail=detail))
