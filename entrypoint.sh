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
for f in config.json state.json day_history.json nest_token.json client_secrets.json tesla_cache.json; do
    if [ ! -e /app/$f ] && [ -f /app/data/$f ]; then
        ln -sf /app/data/$f /app/$f
    fi
done
if [ ! -e /app/agent.log ]; then
    touch /app/logs/agent.log
    ln -sf /app/logs/agent.log /app/agent.log
fi

# ── First-run: Tesla OAuth ────────────────────────────────────────────────
if [ ! -f /app/data/tesla_cache.json ]; then
    if [ -t 0 ]; then
        echo ""
        echo "Tesla token not found — running first-time authorization..."
        echo "A URL will be printed below. Open it in your browser,"
        echo "complete login, then paste the redirect URL back here."
        echo ""
        python -c "
import json, teslapy
from pathlib import Path
config = json.load(open('/app/data/config.json'))
tesla  = teslapy.Tesla(config['tesla_email'], cache_file='/app/data/tesla_cache.json')
if not tesla.authorized:
    tesla.fetch_token()
print('Tesla authorized successfully.')
"
    else
        echo "WARNING: Tesla not authorized. Run 'docker compose up' (without -d) to complete OAuth."
    fi
fi

# ── First-run: Nest OAuth ─────────────────────────────────────────────────
if [ ! -f /app/data/nest_token.json ]; then
    if [ -t 0 ]; then
        echo ""
        echo "Nest token not found — running first-time authorization..."
        echo "A URL will be printed below. Open it in your browser,"
        echo "grant access, then paste the redirect URL back here."
        echo ""
        python -c "
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path
config = json.load(open('/app/data/config.json'))
scopes = ['https://www.googleapis.com/auth/sdm.service']
flow   = InstalledAppFlow.from_client_secrets_file(
    config.get('nest_client_secrets_file', 'client_secrets.json'), scopes
)
creds  = flow.run_console()
Path('/app/data/nest_token.json').write_text(creds.to_json())
print('Nest authorized successfully.')
"
    else
        echo "WARNING: Nest not authorized. Run 'docker compose up' (without -d) to complete OAuth."
    fi
fi

echo ""
echo "Starting web dashboard on :8080"
echo ""

exec uvicorn web:app --host 0.0.0.0 --port 8080
