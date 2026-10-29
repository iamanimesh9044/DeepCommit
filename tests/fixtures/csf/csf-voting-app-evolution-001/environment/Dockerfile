# Agent workspace ("main" service). The agent edits /app; the vote, worker and
# result containers mount the same volume and restart when their code changes.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq procps postgresql-client redis-tools \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests redis psycopg2-binary "python-socketio[client]" websocket-client pytest

WORKDIR /app
COPY app/ /app/
RUN git init -q \
 && git add -A \
 && git -c user.name=csf -c user.email=csf@localhost commit -qm "baseline: dockersamples/example-voting-app@63e9150"
