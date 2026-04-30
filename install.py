#!/usr/bin/env python3
"""
install.py — One-time setup for SRP Energy Agent
Run once after installing dependencies:
  pip install -r requirements.txt
  python install.py
"""
import subprocess
import sys
import os
from pathlib import Path

AGENT_DIR = Path(__file__).parent.resolve()
AGENT_PY  = AGENT_DIR / "agent.py"
PYTHON    = sys.executable

print("SRP Energy Agent — Setup")
print(f"Agent directory : {AGENT_DIR}")
print(f"Python          : {PYTHON}")
print()

# ── Check dependencies ────────────────────────────────────────────────────
deps = ["teslapy", "requests", "google.auth", "google_auth_oauthlib"]
missing = []
for dep in deps:
    try:
        __import__(dep.replace("-", "_"))
    except ImportError:
        missing.append(dep)

if missing:
    print(f"Missing packages: {missing}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

print("✓ All dependencies present")

# ── First-run Tesla auth ──────────────────────────────────────────────────
import json
config_file = AGENT_DIR / "config.json"
if not config_file.exists():
    print("\n⚠  config.json not found")
    print("  cp config.example.json config.json")
    print("  Then edit with your Tesla email + Nest credentials")
    sys.exit(1)

config = json.loads(config_file.read_text())
print(f"\nTesla account: {config.get('tesla_email', '???')}")
print("Initiating Tesla OAuth (browser window may open)...")

import teslapy
tesla = teslapy.Tesla(config["tesla_email"])
if not tesla.authorized:
    tesla.fetch_token()
    print("✓ Tesla token saved")
else:
    print("✓ Tesla already authorized")

# ── Install cron job ──────────────────────────────────────────────────────
print("\nInstalling cron jobs...")

# The cron runs every 5 minutes. The agent internally decides whether to
# take action based on the current phase (peak = 5min, night = 60min, etc.)
# This means the cron fires frequently but the agent skips work when not needed.

cron_line = f"*/5 * * * * {PYTHON} {AGENT_PY} >> {AGENT_DIR}/cron.log 2>&1"

# Check if already installed
existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
if cron_line in existing.stdout:
    print("✓ Cron job already installed")
else:
    new_crontab = existing.stdout.rstrip("\n") + "\n" + cron_line + "\n"
    proc = subprocess.run(
        ["crontab", "-"],
        input=new_crontab,
        capture_output=True,
        text=True
    )
    if proc.returncode == 0:
        print(f"✓ Cron installed: {cron_line}")
    else:
        print(f"✗ Cron install failed: {proc.stderr}")
        print(f"  Add manually to crontab:\n  {cron_line}")

# ── Nest OAuth (interactive) ──────────────────────────────────────────────
print("\nInitiating Nest OAuth...")
print("A browser window will open — log in with your Google account")
print("and grant access to your Nest devices.\n")

nest_token = AGENT_DIR / "nest_token.json"
if nest_token.exists():
    print("✓ Nest token already exists (delete nest_token.json to re-auth)")
else:
    from google_auth_oauthlib.flow import InstalledAppFlow
    secrets_file = config.get("nest_client_secrets_file", "client_secrets.json")
    scopes = ["https://www.googleapis.com/auth/sdm.service"]
    flow  = InstalledAppFlow.from_client_secrets_file(secrets_file, scopes)
    creds = flow.run_local_server(port=0)
    nest_token.write_text(creds.to_json())
    print("✓ Nest token saved")

print("\n✅ Setup complete! The agent will run every 5 minutes via cron.")
print("   Logs: agent.log (agent activity) | cron.log (cron output)")
print("   State: state.json (current state)")
print("   History: day_history.json (learning data)")
print("\n   To run manually:  python agent.py")
print("   To view logs:     tail -f agent.log")
print("   To check cron:    crontab -l")
