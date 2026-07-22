"""Seeds the default Efforti 3-touch sequence on first boot."""
from .models import Sequence, SequenceStep

STEP_0_SUBJECT = "how {{company}} leadership sees team effort"

STEP_0_BODY = """Hi {{first_name}},

{{ opener if opener else "Quick question — when a project at " ~ company ~ " starts slipping, how long before you actually find out?" }}

For most CEOs of 30–200 person teams it's days, sometimes weeks, because dashboards show data, not people. Efforti runs conversational AI check-ins with your team and turns them into a live leadership view: effort allocation, blockers, and risks — before deadlines slip.

One customer's manager dashboard populates within 10 minutes of the first team check-in. Worth a 20-minute look?

Ashutosh Palai
Efforti — Leadership AI for SMB CEOs"""

STEP_1_BODY = """Hi {{first_name}},

Floating this back up. The short version: your tools track tasks; nobody tracks whether your team is actually unblocked and aligned. Efforti does — automatically, without adding another tool for your team to check.

If team visibility isn't a pain right now, tell me and I'll close the loop. If it is, I'll show you a live workspace in 20 minutes.

Ashutosh Palai"""

STEP_2_BODY = """Hi {{first_name}},

Last note from me. If the timing's wrong I'll leave it here — but one thing before I go: we're onboarding a small group of pilot companies this quarter with hands-on setup from our founding team. If seeing where your team's effort actually goes sounds useful in the next few months, reply "later" and I'll check back then.

Either way — good luck with the quarter.

Ashutosh Palai"""


def seed_default_sequence(db):
    if db.query(Sequence).count() > 0:
        return
    seq = Sequence(name="Efforti — CEO cold sequence v1")
    db.add(seq)
    db.flush()
    db.add_all([
        SequenceStep(sequence_id=seq.id, step_index=0, wait_days=0,
                     subject=STEP_0_SUBJECT, body=STEP_0_BODY),
        SequenceStep(sequence_id=seq.id, step_index=1, wait_days=3,
                     subject="", body=STEP_1_BODY),
        SequenceStep(sequence_id=seq.id, step_index=2, wait_days=5,
                     subject="", body=STEP_2_BODY),
    ])
    db.commit()
