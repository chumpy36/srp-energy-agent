#!/bin/bash
# entrypoint.sh — Container startup for SRP Energy Agent
set -e

echo "=== SRP Energy Agent Container Starting ==="
echo "Timezone: $(cat /etc/timezone)"
echo "Time: $(date)"

# ── Check for config ──────────────────────────────────────────────────────
if [ ! -f /app/data/config.json ]; then
    echo ""
    echo "ERROR: /app/data/config.json not found."
    echo ""
    echo "Before starting the container:"
    echo "  cp config.example.json ./data/config.json"
    echo "  nano ./data/config.json   # fill in credentials"
    echo ""
    echo "Then re-run: docker compose up"
    exit 1
fi

# ── Symlink data files (in case volume wasn't mounted at build time) ───────
for f in config.json state.json day_history.json nest_token.json client_secrets.json tesla_tokens.json tesla_private_key.pem tesla_public_key.pem; do
    if [ ! -e /app/$f ] && [ -f /app/data/$f ]; then
        ln -sf /app/data/$f /app/$f
    fi
done
if [ ! -e /app/agent.log ]; then
    touch /app/logs/agent.log
    ln -sf /app/logs/agent.log /app/agent.log
fi

# ── Tesla Fleet API auth note ─────────────────────────────────────────────
if [ ! -f /app/data/tesla_tokens.json ]; then
    echo ""
    echo "NOTE: Tesla Fleet API tokens not found."
    echo "After the dashboard starts, visit:"
    echo "  https://srp.hollandit.work/oauth/tesla/login"
    echo "to complete OAuth. Until then, the agent will skip Powerwall actions."
    echo ""
fi

# ── Nest Fleet OAuth note ─────────────────────────────────────────────────
if [ ! -f /app/data/nest_token.json ]; then
    echo ""
    echo "NOTE: Nest tokens not found."
    echo "After the dashboard starts, visit:"
    echo "  https://srp.hollandit.work/oauth/nest/login"
    echo "to complete OAuth."
    echo ""
fi

echo ""
echo "Starting web dashboard on :8080"
echo ""

exec uvicorn web:app --host 0.0.0.0 --port 8080
