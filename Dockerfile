FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# DRY_RUN stays true until you've warmed your domains; override in the host env.
ENV DRY_RUN=true

# Hosts (Render/Railway/Fly) inject $PORT; fall back to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
