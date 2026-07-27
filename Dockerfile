# Container image for the CRM (works on Render, Railway, Fly.io, etc.)
# Set DATABASE_URL in the host's env to a managed Postgres connection string;
# without it the app falls back to a local SQLite file (dev only).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
EXPOSE 8080

# 1 worker is plenty for a single user.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 app:app"]
