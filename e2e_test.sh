#!/bin/bash
set -e
cd "$(dirname "$0")"
rm -f outreach.db
export DRY_RUN=true

python3 -m uvicorn app.main:app --port 8000 > /tmp/server.log 2>&1 &
SERVER_PID=$!
sleep 4

echo "== boot =="
curl -s -o /dev/null -w "GET / -> %{http_code}\n" http://localhost:8000/

echo "== import csv =="
curl -s -o /dev/null -w "POST /leads/import -> %{http_code}\n" \
  -F "file=@/tmp/sample_leads.csv" -F "mx_check=off" http://localhost:8000/leads/import

echo "== add mailbox =="
curl -s -o /dev/null -w "POST /mailboxes/add -> %{http_code}\n" \
  --data-urlencode "email=piyush@getefforti.com" \
  --data-urlencode "display_name=Piyush from Efforti" \
  --data-urlencode "app_password=test-app-password" \
  --data-urlencode "daily_cap=25" \
  http://localhost:8000/mailboxes/add

echo "== enroll =="
curl -s -o /dev/null -w "POST /leads/enroll -> %{http_code}\n" \
  -d "sequence_id=1" http://localhost:8000/leads/enroll

echo "== force sends due + run cycle (inside server via endpoint) =="
python3 - << 'PYEOF'
import sqlite3
c = sqlite3.connect('outreach.db')
c.execute("UPDATE enrollments SET next_send_at = datetime('now','-1 hour')")
c.commit(); c.close()
PYEOF
# widen business hours for the test cycle (run in-process, same env trick)
DRY_RUN=true python3 - << 'PYEOF'
import app.scheduler as s
s.BUSINESS_START, s.BUSINESS_END = 0, 24
s.process_due_sends()
print("send cycle ran")
PYEOF

echo "== run a SECOND cycle immediately — follow-ups must NOT fire early =="
DRY_RUN=true python3 - << 'PYEOF'
import app.scheduler as s
s.BUSINESS_START, s.BUSINESS_END = 0, 24
s.process_due_sends()
print("second cycle ran")
PYEOF

echo "== unsubscribe flow =="
TOKEN=$(python3 -c "import sqlite3; c=sqlite3.connect('outreach.db'); print(c.execute(\"SELECT unsub_token FROM leads WHERE email='rohan@zylotech.example.com'\").fetchone()[0])")
curl -s -o /dev/null -w "GET /u/token -> %{http_code}\n" http://localhost:8000/u/$TOKEN

echo "== final DB state =="
python3 - << 'PYEOF'
import sqlite3
c = sqlite3.connect('outreach.db')
print("LEADS:")
for r in c.execute("SELECT email, status, verify_result FROM leads"): print("  ", r)
print("ENROLLMENTS:")
for r in c.execute("SELECT id, current_step, status FROM enrollments"): print("  ", r)
print("MESSAGES:")
for r in c.execute("SELECT lead_email, step_index, subject, status FROM messages"): print("  ", r)
print("MAILBOX:", c.execute("SELECT email, sent_today FROM mailboxes").fetchall())
print("SUPPRESSIONS:", c.execute("SELECT email, reason FROM suppressions").fetchall())
print("EVENTS:")
for r in c.execute("SELECT kind, substr(detail,1,90) FROM events ORDER BY id"): print("  ", r)
PYEOF

kill $SERVER_PID 2>/dev/null || true
echo "== DONE =="
