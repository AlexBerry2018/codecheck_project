# The docker CLI is copied from the official image: the grader starts one throw-away
# container per test run through the host's Docker socket (see docker-compose.yml).
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /srv
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY app app
EXPOSE 8000
# one process: the Kafka consumers live in threads of this process, scale with --scale
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
