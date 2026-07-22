#!/bin/bash
# Start the outreach engine. First run creates outreach.db and seeds the sequence.
cd "$(dirname "$0")"
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
