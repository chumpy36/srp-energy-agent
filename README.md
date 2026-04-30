# SRP Energy Agent
### Mesa, AZ — Powerwall 3 + Nest Thermostat Controller
### Customer Generation Plan (solar) — Off-Grid-First Strategy

---

## What this does

Runs every 5 minutes and manages your 16× Silfab 400HC+ solar system,
Tesla Powerwall 3, and two Nest thermostats (Family Room + Guest Wing)
according to SRP's Customer Generation Plan peak schedule.

Core behaviors:
- Stays off-grid during all SRP peak windows (weekdays 2–8pm summer, 5–9am & 5–9pm winter)
- Pre-cools the house before peak using solar + grid (thermal mass strategy)
- Manages battery margin during peak via +1°F setback tiers
- Tops off battery to 100% via grid at cheap off-peak rates after each peak
- Assesses overnight off-grid viability before going grid-free each night
- Learns your actual depletion curve vs daily temperature over time
- Respects guest/SRP manual thermostat overrides as new baselines

---

## Hardware & Accounts Required

- Tesla Powerwall 3 with active Tesla account
- 2× Google Nest thermostats (Family Room, Guest Wing)
- Google account with Nest devices linked
- A machine to run cron (Raspberry Pi 4 recommended, or any always-on computer)
- Python 3.9+

---

## Step 1 — Google Device Access Setup (~15 min, one-time)

1. Go to https://console.nest.google.com/device-access
2. Click "Get started" — there is a one-time **$5 fee** to Google
3. Create a new project, give it any name (e.g. "srp-agent")
4. Note your **Project ID** — you'll need it for config.json

5. Go to https://console.cloud.google.com
6. Create a new project (or use existing)
7. Enable the **Smart Device Management API**
8. Go to Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: Desktop app
   - Download the JSON → save as **client_secrets.json** in this folder

9. In Device Access console, add your OAuth Client ID to your project
   under "OAuth client IDs"

---

## Step 2 — Tesla Setup (~5 min, one-time)

Tesla uses your normal account credentials via teslapy.
No developer account needed for personal use.

Your Tesla email goes in config.json. On first run, teslapy will open
a browser window for you to log in and authorize.

Optional: find your Energy Site ID in the Tesla app:
  App → Energy → [your Powerwall] → Settings icon → the ID is in the URL
Leave tesla_site_id as null to auto-detect it.

---

## Step 3 — Install

```bash
# Clone or copy this folder to your machine
cd srp-agent

# Install Python dependencies
pip install -r requirements.txt

# Copy and edit config
cp config.example.json config.json
nano config.json   # fill in your Tesla email, Nest project ID, etc.

# Run setup (handles Tesla auth, Nest OAuth, cron install)
python install.py
```

---

## Step 4 — Configure Nest Zone Names

In config.json, the `nest_zone_map` maps Nest device display names to zone IDs.
The match is a case-insensitive substring, so "Family" will match
"Family Room Thermostat" or "Main Family Area".

Check your device names:
```bash
python -c "
from agent import get_nest_state
import json
config = json.load(open('config.json'))
print(json.dumps(get_nest_state(config), indent=2))
"
```

Adjust `nest_zone_map` in config.json to match your actual device names.

---

## Step 5 — Test Run

```bash
python agent.py
```

Watch agent.log for output:
```bash
tail -f agent.log
```

Example output:
```
2025-08-01 14:05:01 [INFO] === Agent cycle 2025-08-01 14:05 ===
2025-08-01 14:05:02 [INFO] Powerwall: 88% | Solar 4.2kW | Load 3.8kW | Grid -0.4kW
2025-08-01 14:05:03 [INFO] Nest [family] Family Room: current 77.1°F, set 76°F
2025-08-01 14:05:03 [INFO] Nest [guest] Guest Wing: current 76.4°F, set 76°F
2025-08-01 14:05:04 [INFO] Tomorrow forecast high: 108°F
2025-08-01 14:05:04 [INFO]   ☀️  Solar-assisted peak | 4.2kW solar, net draw 0.4kW | Margin 6.1kWh → Ample
2025-08-01 14:05:04 [INFO]   ✅ No setback needed — holding at current baselines (family 76°F, guest 76°F)
2025-08-01 14:05:04 [INFO]   Lever: none | Grid: False | targetPrePeak: 87% (base 85% + hold adj 0%)
2025-08-01 14:05:04 [INFO] Powerwall mode → backup | reserve floor 15%
2025-08-01 14:05:04 [INFO] Nest [family] already at 76°F — no command needed
2025-08-01 14:05:04 [INFO] Nest [guest] already at 76°F — no command needed
2025-08-01 14:05:04 [INFO] === Cycle complete — next poll in 10 min ===
```

---

## Cron Schedule

The install script sets up:
```
*/5 * * * * python /path/to/agent.py
```

The agent fires every 5 minutes but only takes action when needed.
During peak (5-min polling) it actively monitors and may push commands.
During the night (60-min polling) it checks battery and skips if stable.

---

## Files

| File | Purpose |
|------|---------|
| agent.py | Main agent script |
| config.json | Your credentials and zone map |
| state.json | Current agent state (battery, holds, day record) |
| day_history.json | Learning model — 60 days of peak depletion data |
| agent.log | Detailed activity log |
| nest_token.json | Nest OAuth token (auto-managed) |

---

## Manual Hold

The agent automatically detects when a Nest setpoint is changed manually
(guest comfort adjustment or SRP demand event). It accepts the new
temperature as the baseline for that zone and recalculates all battery
math accordingly.

Holds clear automatically at:
- 9pm (day → night transition)
- 5am (night → day transition)

---

## Learning Model

After 5+ days of data, the agent fits a linear regression:
  net kWh depleted during peak ~ daily high °F

It then uses tomorrow's forecast temperature (from Open-Meteo, free, no key)
to predict how much battery you'll need and sets targetPrePeak accordingly.
The model is stored in day_history.json and improves over time.

---

## Troubleshooting

**Tesla auth fails:**
  Delete `~/.cache/teslapy/` and re-run install.py

**Nest devices not found:**
  Run the diagnostic command in Step 4 to see actual device names,
  then update nest_zone_map in config.json

**Weather API fails:**
  Open-Meteo is free but occasionally unavailable.
  The agent falls back to yesterday's temperature if the API is unreachable.

**Powerwall mode not changing:**
  Verify tesla_site_id is correct. Some Powerwall installations require
  the site ID to be set explicitly rather than auto-detected.
