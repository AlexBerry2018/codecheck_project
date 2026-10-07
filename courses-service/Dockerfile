FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY app app
COPY wsgi.py .
RUN useradd --system --uid 10001 appuser
USER appuser
EXPOSE 8000
# One process with threads: the outbox relay thread and the Prometheus counters live in
# that process. Scale with `docker compose up --scale courses-service=N` instead.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "8", "--timeout", "60", "--bind", "0.0.0.0:8000", "wsgi:app"]
