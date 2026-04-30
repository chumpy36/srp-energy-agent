FROM python:3.11-slim

LABEL maintainer="srp-energy-agent"
LABEL description="SRP Powerwall + Nest Energy Agent — Mesa, AZ"

# ── System dependencies ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Timezone: Mesa AZ (no DST) ────────────────────────────────────────────
ENV TZ=America/Phoenix
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ── App directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy source ───────────────────────────────────────────────────────────
COPY agent.py .
COPY web.py .
COPY tesla_fleet.py .
COPY templates/ templates/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# ── Persistent data volumes ───────────────────────────────────────────────
# /app/data   → config.json, credentials, state, history, tesla_cache.json
# /app/logs   → agent.log
RUN mkdir -p /app/data /app/logs

# Symlink data files into /app so agent.py finds them at relative paths
RUN ln -s /app/data/config.json          /app/config.json          ; true
RUN ln -s /app/data/state.json           /app/state.json           ; true
RUN ln -s /app/data/day_history.json     /app/day_history.json     ; true
RUN ln -s /app/data/nest_token.json      /app/nest_token.json      ; true
RUN ln -s /app/data/client_secrets.json  /app/client_secrets.json  ; true
RUN ln -s /app/data/tesla_cache.json     /app/tesla_cache.json     ; true
RUN ln -s /app/logs/agent.log            /app/agent.log            ; true

VOLUME ["/app/data", "/app/logs"]

EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
