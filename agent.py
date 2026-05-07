#!/usr/bin/env python3
"""
SRP Powerwall + Nest Energy Agent
Mesa, AZ — Customer Generation Plan
------------------------------------
Runs every 5–15 min via cron. On each cycle it:
  1. Reads Powerwall state (battery %, solar kW, grid draw)
  2. Reads Nest thermostat temps for Family Room + Guest Wing
  3. Determines the current operating phase
  4. Applies two-lever logic (Lever 1: grid charge, Lever 2: temperature)
  5. Pushes thermostat commands if needed
  6. Records daily depletion data for the learning model
  7. Logs everything to agent.log and state.json

Requirements:
  pip install teslapy requests google-auth google-auth-oauthlib pytz schedule
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from tesla_fleet import TeslaFleet
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# ─── PATHS ───────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "state.json"
HISTORY_FILE= BASE_DIR / "day_history.json"
LOG_FILE    = BASE_DIR / "agent.log"
TOKEN_FILE  = BASE_DIR / "nest_token.json"

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("srp-agent")

# ─── TIMEZONE ────────────────────────────────────────────────────────────────
TZ = ZoneInfo("America/Phoenix")   # Mesa AZ — no DST

# ═══════════════════════════════════════════════════════════════════════════
# SYSTEM CONSTANTS  (mirror of the dashboard)
# ═══════════════════════════════════════════════════════════════════════════
BATTERY_KWH   = 13.5   # Powerwall 3 usable capacity
BATTERY_FLOOR = 15     # % — never drain below this

# Comfort setpoints
DAY_COMFORT   = 76     # 5am–9pm
NIGHT_COMFORT = 73     # 9pm–5am
MAX_SETBACK   = 4      # °F above baseline, normal peak
CRIT_SETBACK  = 6      # °F above baseline, critical
MAX_PRECOOL   = 5      # °F below baseline, pre-peak

# SRP Customer Generation Plan peak windows (weekdays only; weekends always off-peak)
# Season determined by month:
#   WINTER      = Nov–Apr  → peaks 5–9am & 5–9pm
#   SUMMER      = May,Jun,Sep,Oct → peak 2–8pm
#   SUMMER_PEAK = Jul–Aug  → peak 2–8pm, highest demand charge

# Powerwall 3 AC charge rate from grid
GRID_CHARGE_KW = 5.0

# Safety buffer added on top of predicted depletion for targetPrePeak
SAFETY_BUFFER_PCT = 12

# Minimum days of history before learning model activates
MIN_LEARNING_SAMPLES = 5

# ═══════════════════════════════════════════════════════════════════════════
# SRP SCHEDULE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_season(month: int) -> str:
    if month in (7, 8):          return "SUMMER_PEAK"
    if month in (5, 6, 9, 10):   return "SUMMER"
    return "WINTER"

def is_peak(dt: datetime) -> bool:
    """True if dt falls in an SRP on-peak window (weekdays only)."""
    if dt.weekday() >= 5:        # Saturday=5, Sunday=6
        return False
    h = dt.hour
    s = get_season(dt.month)
    if s == "WINTER":
        return (5 <= h < 9) or (17 <= h < 21)
    return 14 <= h < 20          # SUMMER and SUMMER_PEAK

def is_pre_peak(dt: datetime) -> bool:
    """True if dt is in the 2-hour preparation window before a peak."""
    if dt.weekday() >= 5 or is_peak(dt):
        return False
    h = dt.hour
    s = get_season(dt.month)
    if s == "WINTER":
        return (3 <= h < 5) or (15 <= h < 17)
    return 12 <= h < 14

def is_top_off_window(dt: datetime) -> bool:
    """True if dt is in the post-peak grid top-off window.

    9–11pm year-round, aligned with the 9pm day→night comfort drop
    (76°F → 73°F) so grid assists during the heavy cooling stage.
    """
    if dt.weekday() >= 5:
        return False
    return 21 <= dt.hour < 23
    # Winter morning peak → no grid top-off (solar handles recharge)

def is_nighttime(dt: datetime) -> bool:
    return dt.hour >= 21 or dt.hour < 5

def comfort_temp(dt: datetime) -> int:
    return NIGHT_COMFORT if is_nighttime(dt) else DAY_COMFORT

def get_peak_sub_phase(dt: datetime) -> str | None:
    """Classify the current sub-phase within a peak window."""
    if not is_peak(dt):
        return None
    s = get_season(dt.month)
    if s == "WINTER":
        return "morning_peak" if dt.hour < 9 else "evening_peak"
    return "solar_supported_peak" if dt.hour < 17 else "solar_gone_peak"

def poll_interval_minutes(dt: datetime) -> int:
    """How often the agent should run checks given current phase."""
    sub = get_peak_sub_phase(dt)
    if sub == "solar_gone_peak":   return 5
    if sub == "solar_supported_peak": return 10
    if sub in ("morning_peak", "evening_peak"): return 10
    if is_pre_peak(dt):            return 15
    if is_nighttime(dt):           return 60
    return 15

def hours_until_next_peak(dt: datetime) -> float:
    """Scan forward up to 24h to find hours until next peak starts."""
    for i in range(1, 145):          # 10-min steps × 144 = 24h
        candidate = dt + timedelta(minutes=i * 10)
        if is_peak(candidate) and not is_peak(dt):
            return i * 10 / 60
    return 24.0

# ═══════════════════════════════════════════════════════════════════════════
# SOLAR OUTPUT MODEL  (Silfab 400HC+ × 16, Mesa AZ)
# ═══════════════════════════════════════════════════════════════════════════
DC_CAPACITY_KW = 6.4      # 16 × 400W
TEMP_COEFF     = -0.0036  # -0.36%/°C
SOLAR_START    = 7.5      # effective generation start (decimal hour)
SOLAR_END      = 17.5     # effective generation end

def calc_solar_kw(hour_frac: float, outside_f: float) -> float:
    """Estimate solar output at a given fractional hour and outside temperature."""
    if hour_frac < SOLAR_START or hour_frac >= SOLAR_END:
        return 0.0
    progress    = (hour_frac - SOLAR_START) / (SOLAR_END - SOLAR_START)
    curve       = math.sin(progress * math.pi)
    outside_c   = (outside_f - 32) * 5 / 9
    panel_c     = outside_c + 25
    temp_derate = 1 + TEMP_COEFF * (panel_c - 25)
    return max(0.0, round(DC_CAPACITY_KW * curve * temp_derate * 0.80, 2))

def theoretical_peak_solar_kwh(outside_f: float) -> float:
    """Clear-sky solar kWh expected during the 2–8pm peak window."""
    total = sum(calc_solar_kw(h, outside_f) for h in range(14, 20))
    return max(0.1, total)

# ═══════════════════════════════════════════════════════════════════════════
# LEARNING MODEL  (linear regression, temperature → net depletion)
# ═══════════════════════════════════════════════════════════════════════════

def linear_regression(points: list[dict]) -> dict | None:
    """Fit y = slope*x + intercept. points = [{"x": ..., "y": ...}, ...]"""
    n = len(points)
    if n < 2:
        return None
    sx  = sum(p["x"] for p in points)
    sy  = sum(p["y"] for p in points)
    sxy = sum(p["x"] * p["y"] for p in points)
    sx2 = sum(p["x"] ** 2 for p in points)
    d   = n * sx2 - sx * sx
    if abs(d) < 1e-10:
        return None
    slope     = (n * sxy - sx * sy) / d
    intercept = (sy - slope * sx) / n
    y_mean    = sy / n
    ss_tot = sum((p["y"] - y_mean) ** 2 for p in points)
    ss_res = sum((p["y"] - (slope * p["x"] + intercept)) ** 2 for p in points)
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-10 else 1.0
    return {"slope": round(slope, 4), "intercept": round(intercept, 3), "r2": round(r2, 3)}

def build_regressions(history: list[dict], season: str) -> dict:
    """Return {"net": regression|None, "solar": regression|None, "n": int}"""
    relevant = [
        d for d in history
        if d.get("season") == season
        and d.get("net_kwh_depleted") is not None
        and d.get("solar_kwh_peak") is not None
    ]
    if len(relevant) < MIN_LEARNING_SAMPLES:
        return {"net": None, "solar": None, "n": len(relevant)}
    return {
        "net":   linear_regression([{"x": d["day_high_f"], "y": d["net_kwh_depleted"]} for d in relevant]),
        "solar": linear_regression([{"x": d["day_high_f"], "y": d["solar_kwh_peak"]}   for d in relevant]),
        "n":     len(relevant),
    }

def predict_target(regressions: dict, forecast_f: float, season: str) -> int:
    """Predict targetPrePeak % for tomorrow given forecast temperature."""
    fallback = {"SUMMER_PEAK": 92, "SUMMER": 85, "WINTER": 78}[season]
    if not regressions.get("net"):
        return fallback
    reg = regressions["net"]
    proj_kwh = max(0, reg["slope"] * forecast_f + reg["intercept"])
    proj_pct = proj_kwh / BATTERY_KWH * 100
    target   = proj_pct + SAFETY_BUFFER_PCT
    return round(max(70, min(97, target)))

# ═══════════════════════════════════════════════════════════════════════════
# MARGIN TIERS  (maps kWh buffer → thermostat setback delta)
# ═══════════════════════════════════════════════════════════════════════════

def calc_margin_tier(kwh_margin: float, battery_pct: float) -> dict:
    if battery_pct <= BATTERY_FLOOR + 5:
        return {"setback": CRIT_SETBACK,  "label": "Critical",    "color": "RED"}
    if kwh_margin < 0.5:
        return {"setback": MAX_SETBACK,   "label": "Tight",       "color": "RED"}
    if kwh_margin < 1.5:
        return {"setback": 3,             "label": "Watchful",    "color": "ORANGE"}
    if kwh_margin < 3.0:
        return {"setback": 2,             "label": "Cautious",    "color": "YELLOW"}
    if kwh_margin < 5.0:
        return {"setback": 1,             "label": "Comfortable", "color": "YELLOW"}
    return          {"setback": 0,        "label": "Ample",       "color": "GREEN"}

# ═══════════════════════════════════════════════════════════════════════════
# OVERNIGHT VIABILITY
# ═══════════════════════════════════════════════════════════════════════════

def assess_overnight(battery_pct: float, season: str) -> dict:
    hrs_to_solar  = 24 - 21 + SOLAR_START   # 9pm → 7:30am ≈ 10.5h
    night_load_kw = 0.9 if season == "WINTER" else 1.3
    kwh_needed    = hrs_to_solar * night_load_kw
    kwh_avail     = ((battery_pct - BATTERY_FLOOR) / 100) * BATTERY_KWH
    margin        = kwh_avail - kwh_needed
    return {
        "can_go_off_grid": margin > 1.5,
        "kwh_needed":  round(kwh_needed, 1),
        "kwh_avail":   round(kwh_avail, 1),
        "margin":      round(margin, 1),
        "hrs_to_solar": round(hrs_to_solar, 1),
    }

# ═══════════════════════════════════════════════════════════════════════════
# AGENT DECISION ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def hvac_load_kw(season: str) -> float:
    """Base HVAC load per zone at comfort temp."""
    return {"SUMMER_PEAK": 2.6, "SUMMER": 2.0, "WINTER": 1.2}[season]

def run_agent(state: dict, config: dict, history: list[dict]) -> dict:
    """
    Core decision function. Returns a dict with:
      thermostat_targets   — {zone_id: temp_f}
      grid_allowed         — bool
      lever                — "grid" | "temp" | "both" | "none"
      decisions            — list of human-readable strings
      poll_interval_mins   — int
      target_pre_peak      — int (%)
    """
    now         = datetime.now(TZ)
    season      = get_season(now.month)
    sub_phase   = get_peak_sub_phase(now)
    peak        = is_peak(now)
    pre_peak    = is_pre_peak(now)
    top_off_win = is_top_off_window(now)
    night       = is_nighttime(now)
    hour_frac   = now.hour + now.minute / 60

    battery_pct  = state["battery_pct"]
    outside_f    = state["outside_temp_f"]
    solar_kw     = state.get("solar_kw", calc_solar_kw(hour_frac, outside_f))
    forecast_f   = state.get("forecast_temp_f", outside_f)
    hold_state   = state.get("hold_state", {
        "family": {"type": "agent", "held_temp": comfort_temp(now)},
        "guest":  {"type": "agent", "held_temp": comfort_temp(now)},
    })

    # Baseline comfort
    default_comfort = comfort_temp(now)

    # Per-zone baselines (held temp or schedule default)
    zone_baselines = {
        z: (hold_state[z]["held_temp"]
            if hold_state.get(z, {}).get("type", "agent") != "agent"
            else default_comfort)
        for z in ("family", "guest")
    }
    effective_comfort = (zone_baselines["family"] + zone_baselines["guest"]) / 2

    # Hold load extra — extra kW vs schedule default
    hvac_base = hvac_load_kw(season)
    hold_load_extra = sum(
        max(0, (default_comfort - zone_baselines[z]) * hvac_base * 0.05)
        for z in ("family", "guest")
    )

    # Learning model → target pre-peak
    regressions   = build_regressions(history, season)
    static_target = {"SUMMER_PEAK": 92, "SUMMER": 85, "WINTER": 78}[season]
    base_target   = predict_target(regressions, forecast_f, season)
    hold_adj      = round(min(10, hold_load_extra * 5))
    target_pp     = min(97, base_target + hold_adj)

    # Battery math
    batt_floor  = BATTERY_FLOOR
    batt_usable = ((battery_pct - batt_floor) / 100) * BATTERY_KWH
    batt_low    = battery_pct <= batt_floor + 5
    batt_good   = battery_pct >= target_pp

    # Total load at a given delta from zone baselines
    def total_load(delta: float) -> float:
        return sum(
            max(0.3, hvac_base * (1 + delta * 0.05))
            for _ in ("family", "guest")
        )

    decisions     = []
    thermo_delta  = 0       # °F above/below each zone's baseline
    grid_allowed  = False
    lever         = "none"
    target_reserve = BATTERY_FLOOR  # Powerwall reserve % to enforce this cycle
    margin_tier   = {"setback": 0, "label": "Ample", "color": "GREEN"}
    poll_mins     = poll_interval_minutes(now)

    hold_note = (
        f" | Hold: family={zone_baselines['family']}°F guest={zone_baselines['guest']}°F"
        if any(hold_state.get(z, {}).get("type", "agent") != "agent" for z in ("family", "guest"))
        else ""
    )

    # ── PEAK ────────────────────────────────────────────────────────────────
    if peak:
        grid_allowed = False
        lever        = "temp"

        if sub_phase == "solar_supported_peak":
            net_draw         = total_load(0) - solar_kw
            hrs_left         = 20 - now.hour
            solar_assist_hrs = max(0, 17 - now.hour)
            solar_gone_hrs   = max(0, hrs_left - solar_assist_hrs)
            kwh_from_batt    = max(0, net_draw * solar_assist_hrs) + total_load(0) * solar_gone_hrs
            kwh_margin       = batt_usable - kwh_from_batt
            margin_tier      = calc_margin_tier(kwh_margin, battery_pct)
            thermo_delta     = margin_tier["setback"]
            net_lbl = f"net-charging +{abs(net_draw):.1f}kW" if net_draw <= 0 else f"net draw {net_draw:.1f}kW"
            decisions.append(f"☀️  Solar-assisted peak | {solar_kw:.1f}kW solar, {net_lbl} | Margin {kwh_margin:.1f}kWh → {margin_tier['label']}{hold_note}")
            if margin_tier["setback"] > 0:
                decisions.append(f"🌡️  +{margin_tier['setback']}°F from baseline → family {zone_baselines['family']+thermo_delta}°F, guest {zone_baselines['guest']+thermo_delta}°F")

        elif sub_phase == "solar_gone_peak":
            hrs_left    = 20 - now.hour
            kwh_needed  = total_load(0) * hrs_left
            kwh_margin  = batt_usable - kwh_needed
            margin_tier = calc_margin_tier(kwh_margin, battery_pct)
            thermo_delta = margin_tier["setback"]
            decisions.append(f"🔋 Solar-gone peak | {hrs_left:.1f}h remain | {batt_usable:.1f}kWh avail vs {kwh_needed:.1f}kWh needed | {margin_tier['label']}{hold_note}")
            decisions.append(f"🌡️  +{margin_tier['setback']}°F → family {zone_baselines['family']+thermo_delta}°F, guest {zone_baselines['guest']+thermo_delta}°F | Every {poll_mins}min")

        else:  # Winter morning or evening peak
            hrs_left    = (9 if sub_phase == "morning_peak" else 21) - now.hour
            kwh_needed  = total_load(0) * hrs_left
            kwh_margin  = batt_usable - kwh_needed
            margin_tier = calc_margin_tier(kwh_margin, battery_pct)
            thermo_delta = margin_tier["setback"]
            decisions.append(f"⚡ Winter {sub_phase} | {hrs_left}h left | Margin {kwh_margin:.1f}kWh → {margin_tier['label']}{hold_note}")
            decisions.append(f"🌡️  family {zone_baselines['family']+thermo_delta}°F, guest {zone_baselines['guest']+thermo_delta}°F (+{margin_tier['setback']}°)")

    # ── PRE-PEAK ─────────────────────────────────────────────────────────────
    elif pre_peak:
        if not batt_good:
            lever        = "both"
            grid_allowed = True
            target_reserve = target_pp
            pre_cool_deg = min(MAX_PRECOOL, 4 if solar_kw > 2 else 3)
            thermo_delta = -pre_cool_deg
            kwh_deficit  = (target_pp - battery_pct) / 100 * BATTERY_KWH
            decisions.append(f"🔋 Lever 1 (grid): Charging to {target_pp}% | {battery_pct:.0f}% now | {kwh_deficit:.1f}kWh needed{hold_note}")
            decisions.append(f"🌡️  Lever 2 (pre-cool): family {zone_baselines['family']+thermo_delta}°F, guest {zone_baselines['guest']+thermo_delta}°F ({pre_cool_deg}°F below baseline)")
        else:
            lever        = "temp"
            thermo_delta = -min(2, MAX_PRECOOL)
            decisions.append(f"✅ Battery ready ({battery_pct:.0f}%) — gentle pre-cool{hold_note}")
            decisions.append(f"🌡️  family {zone_baselines['family']+thermo_delta}°F, guest {zone_baselines['guest']+thermo_delta}°F")

    # ── DAILY TOP-OFF ────────────────────────────────────────────────────────
    elif top_off_win:
        top_off_done = battery_pct >= 99.5
        lever        = "none" if top_off_done else "grid"
        thermo_delta = 0
        if not top_off_done:
            grid_allowed   = True
            target_reserve = 100
            kwh_to_full    = max(0, (100 - battery_pct) / 100 * BATTERY_KWH)
            mins_to_full  = math.ceil(kwh_to_full / GRID_CHARGE_KW * 60)
            energy_rate   = 7.30 if season != "WINTER" else 7.38
            decisions.append(f"🔌 Daily top-off to 100% | {battery_pct:.0f}% now | {kwh_to_full:.1f}kWh | ~{mins_to_full}min | {energy_rate}¢/kWh")
        else:
            og = assess_overnight(battery_pct, season)
            grid_allowed = not og["can_go_off_grid"]
            if og["can_go_off_grid"]:
                decisions.append(f"✅ Top-off complete | Off-grid overnight GO | {og['margin']}kWh margin")
            else:
                decisions.append(f"🔌 Top-off complete | Grid overnight (margin only {og['margin']}kWh)")

    # ── NIGHTTIME ────────────────────────────────────────────────────────────
    elif night:
        thermo_delta = 0    # night comfort is the baseline
        og = assess_overnight(battery_pct, season)
        if og["can_go_off_grid"]:
            lever = "none"
            grid_allowed = False
            decisions.append(f"🌙 Off-grid overnight | {og['kwh_avail']}kWh avail vs {og['kwh_needed']}kWh needed | {og['margin']}kWh margin")
        else:
            lever = "grid"
            grid_allowed = True
            decisions.append(f"🔌 Lever 1 (grid): Overnight assist | margin {og['margin']}kWh insufficient")

    # ── SOLAR WINDOW ─────────────────────────────────────────────────────────
    elif solar_kw > 0:
        thermo_delta = 0
        hrs_until_pk = hours_until_next_peak(now)
        # Simple solar forecast: sum remaining solar before peak
        solar_projected = sum(
            calc_solar_kw(hour_frac + i * 0.25, outside_f)
            for i in range(int(hrs_until_pk * 4))
        ) * 0.25  # kWh
        target_kwh = target_pp / 100 * BATTERY_KWH
        current_kwh = battery_pct / 100 * BATTERY_KWH
        deficit = max(0, target_kwh - current_kwh - solar_projected)
        # Solar primary, grid only if projections fall meaningfully short.
        # 1.5 kWh ≈ 11% of battery — prevents trigger-happy grid pulls on
        # normal morning ramp-up. Pre-peak window (12–2pm) backstops if
        # solar genuinely fails. Drop back to 0.3 to revert.
        if not batt_good and deficit > 1.5:
            lever = "grid"
            grid_allowed = True
            target_reserve = target_pp
            decisions.append(f"⚡ Lever 1 (grid): Solar projects {solar_projected:.1f}kWh short by {deficit:.1f}kWh | {hrs_until_pk:.1f}h until peak")
        else:
            lever = "none"
            decisions.append(f"☀️  Solar {solar_kw:.1f}kW | {battery_pct:.0f}% → {target_pp}% | {'on track' if not batt_good else 'battery ready'}{hold_note}")

    # ── POST-PEAK COAST (summer 8–9pm gap before top-off) ────────────────────
    # Summer peak ends 8pm, top-off starts 9pm. Without this branch the
    # OFF-PEAK DEFAULT would see battery low (just rode out peak) and force
    # Lever 1 grid-charge to target_pp — pulling 5 kW an hour before top-off
    # would have done it cleanly. Coast in self_consumption with reserve at
    # BATTERY_FLOOR and let top-off handle the refill.
    elif season in ("SUMMER", "SUMMER_PEAK") and now.hour == 20:
        thermo_delta = 0
        lever = "none"
        grid_allowed = False
        decisions.append(f"😌 Post-peak coast | battery {battery_pct:.0f}% | top-off in <1h{hold_note}")

    # ── OFF-PEAK DEFAULT ──────────────────────────────────────────────────────
    else:
        thermo_delta = 0
        hrs_until_pk = hours_until_next_peak(now)
        target_kwh   = target_pp / 100 * BATTERY_KWH
        current_kwh  = battery_pct / 100 * BATTERY_KWH
        solar_proj   = sum(
            calc_solar_kw(hour_frac + i * 0.25, outside_f)
            for i in range(int(hrs_until_pk * 4))
        ) * 0.25
        deficit = max(0, target_kwh - current_kwh - solar_proj)
        if not batt_good and deficit > 0.3:
            lever          = "grid"
            grid_allowed   = True
            target_reserve = target_pp
            mins_needed    = math.ceil(deficit / GRID_CHARGE_KW * 60)
            decisions.append(f"🔌 Lever 1 (grid): {battery_pct:.0f}% needs {deficit:.1f}kWh more for {target_pp}% target | {mins_needed}min grid{hold_note}")
        else:
            lever = "none"
            grid_allowed = False
            decisions.append(f"😌 Off-peak | battery {battery_pct:.0f}% | {'on track' if not batt_good else 'ready'} | levers idle{hold_note}")

    # ── Build per-zone thermostat targets ─────────────────────────────────────
    thermostat_targets = {}
    for zone_id in ("family", "guest"):
        base = zone_baselines[zone_id]
        raw  = base + thermo_delta
        thermostat_targets[zone_id] = max(NIGHT_COMFORT - 1, min(DAY_COMFORT + CRIT_SETBACK, round(raw)))

    return {
        "thermostat_targets":  thermostat_targets,
        "thermo_delta":        thermo_delta,
        "zone_baselines":      zone_baselines,
        "grid_allowed":        grid_allowed,
        "lever":               lever,
        "margin_tier":         margin_tier,
        "decisions":           decisions,
        "poll_interval_mins":  poll_mins,
        "target_pre_peak":     target_pp,
        "target_reserve":      target_reserve,
        "base_target":         base_target,
        "hold_adj":            hold_adj,
        "season":              season,
        "sub_phase":           sub_phase,
        "is_peak":             peak,
        "is_pre_peak":         pre_peak,
        "is_top_off":          top_off_win,
        "is_night":            night,
        "solar_kw":            solar_kw,
        "timestamp":           now.isoformat(),
    }

# ═══════════════════════════════════════════════════════════════════════════
# TESLA POWERWALL API
# ═══════════════════════════════════════════════════════════════════════════

def get_powerwall_state(tesla: TeslaFleet, config: dict) -> dict:
    """
    Fetch live Powerwall state. Returns:
      battery_pct, solar_kw, load_kw, grid_kw, grid_active
    """
    if tesla is None:
        return {}
    try:
        products = tesla.api("PRODUCT_LIST")["response"]
        site_id  = None
        for p in products:
            if p.get("resource_type") == "battery":
                site_id = p["energy_site_id"]
                break

        if site_id is None:
            # Fall back to config
            site_id = config.get("tesla_site_id")

        data = tesla.api("SITE_DATA", path_vars={"site_id": site_id})["response"]

        battery_pct = data.get("percentage_charged", 0)
        solar_kw    = data.get("solar_power", 0) / 1000
        load_kw     = data.get("load_power", 0) / 1000
        grid_kw     = data.get("grid_power", 0) / 1000   # positive = importing
        grid_active = data.get("grid_status") == "Active"

        log.info(f"Powerwall: {battery_pct:.0f}% | Solar {solar_kw:.1f}kW | Load {load_kw:.1f}kW | Grid {grid_kw:+.1f}kW")
        return {
            "battery_pct":  round(battery_pct, 1),
            "solar_kw":     round(solar_kw, 2),
            "load_kw":      round(load_kw, 2),
            "grid_kw":      round(grid_kw, 2),
            "grid_active":  grid_active,
        }
    except Exception as e:
        log.error(f"Powerwall API error: {e}")
        return {}

def set_powerwall_mode(tesla: TeslaFleet, config: dict, grid_allowed: bool,
                       battery_pct: float, target_reserve: int = BATTERY_FLOOR) -> None:
    """
    Set Powerwall operating mode + Lever 1 (grid charging).

    Operating mode flips between two states:
      - autonomous (Time-Based Control) during active grid-charge windows.
        Self-Powered throttles grid import to ~1.7 kW; autonomous lets the
        Powerwall pull at full ~5 kW so reserve raises actually fill the battery.
      - self_consumption otherwise — the only mode where the battery actually
        discharges to cover home load. Tesla's "backup" mode preserves battery
        for outages (opposite of what SRP optimization needs).

    Active grid-charge = Lever 1 firing AND target_reserve above BATTERY_FLOOR.
    Pre-peak / top-off / solar deficit / off-peak deficit branches qualify;
    overnight assist (target_reserve = floor) does not.

    Lever 1 implementation: when grid_allowed and battery is below target_reserve,
    raise backup_reserve_percent to target_reserve. Powerwall treats that as a
    must-hold reserve and pulls from grid+solar to reach it.
    """
    if tesla is None:
        log.warning("Tesla not authorized — skipping Powerwall mode set")
        return
    try:
        site_id = config.get("tesla_site_id")
        if not site_id:
            products = tesla.api("PRODUCT_LIST")["response"]
            for p in products:
                if p.get("resource_type") == "battery":
                    site_id = p["energy_site_id"]
                    break
        if not site_id:
            log.error("Powerwall site_id not found — cannot set mode")
            return

        # Lever 1: force grid-charge to target_reserve when branch wants it
        actively_charging = grid_allowed and battery_pct < target_reserve and target_reserve > BATTERY_FLOOR
        if actively_charging:
            reserve = min(100, int(target_reserve))
            log.info(f"Lever 1 active: forcing grid-charge — reserve {reserve}% (battery at {battery_pct:.0f}%, target {target_reserve}%)")
        else:
            reserve = BATTERY_FLOOR

        tesla.api("BATTERY_BACKUP_RESERVE",
                  path_vars={"site_id": site_id},
                  backup_reserve_percent=reserve)

        # autonomous unlocks ~5 kW grid charging; self_consumption throttles to ~1.7 kW
        op_mode = "autonomous" if actively_charging else "self_consumption"
        tesla.api("BATTERY_OPERATION_MODE",
                  path_vars={"site_id": site_id},
                  default_real_mode=op_mode)

        log.info(f"Powerwall mode → {op_mode} | reserve {reserve}% | grid_allowed={grid_allowed}")
    except Exception as e:
        log.error(f"Powerwall set mode error: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# GOOGLE NEST SDM API
# ═══════════════════════════════════════════════════════════════════════════
NEST_SCOPES = ["https://www.googleapis.com/auth/sdm.service"]

def get_nest_credentials(config: dict) -> Credentials:
    """Load or refresh OAuth2 credentials for Nest SDM."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), NEST_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config["nest_client_secrets_file"], NEST_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

def get_nest_state(config: dict) -> dict:
    """
    Fetch current thermostat state for all Nest devices.
    Returns {zone_id: {"current_temp_f": float, "set_temp_f": float, "device_name": str}}
    """
    try:
        creds    = get_nest_credentials(config)
        headers  = {"Authorization": f"Bearer {creds.token}"}
        project  = config["nest_project_id"]
        url      = f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{project}/devices"
        resp     = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        devices  = resp.json().get("devices", [])
        result   = {}
        zone_map = config.get("nest_zone_map", {})  # {"device_name_fragment": "family"|"guest"}

        for device in devices:
            traits    = device.get("traits", {})
            temp_trait = traits.get("sdm.devices.traits.Temperature", {})
            heat_trait = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
            info_trait = traits.get("sdm.devices.traits.Info", {})
            eco_trait  = traits.get("sdm.devices.traits.ThermostatEco", {})
            mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
            eco_active = eco_trait.get("mode", "OFF") != "OFF"
            hvac_mode  = mode_trait.get("mode", "OFF")

            name_display = info_trait.get("customName") or ""
            if not name_display:
                # Fall back to the parent room's displayName (e.g. "Family Room")
                for rel in device.get("parentRelations", []):
                    if rel.get("displayName"):
                        name_display = rel["displayName"]
                        break
            if not name_display:
                name_display = device["name"]
            ambient_c    = temp_trait.get("ambientTemperatureCelsius", 0)
            heat_c       = heat_trait.get("heatCelsius", heat_trait.get("coolCelsius", 0))

            ambient_f = round(ambient_c * 9/5 + 32, 1)
            set_f     = round(heat_c * 9/5 + 32, 1)

            # Map device to zone id via config
            zone_id = None
            for fragment, zid in zone_map.items():
                if fragment.lower() in name_display.lower():
                    zone_id = zid
                    break

            if zone_id:
                result[zone_id] = {
                    "current_temp_f": ambient_f,
                    "set_temp_f":     set_f,
                    "device_name":    name_display,
                    "device_id":      device["name"],
                    "eco_active":     eco_active,
                    "hvac_mode":      hvac_mode,
                }
                eco_tag = " [ECO]" if eco_active else ""
                log.info(f"Nest [{zone_id}] {name_display}: current {ambient_f}°F, set {set_f}°F, mode {hvac_mode}{eco_tag}")

        return result
    except Exception as e:
        log.error(f"Nest GET error: {e}")
        return {}

SETPOINT_COOL_MIN = 65
SETPOINT_COOL_MAX = 80
SETPOINT_HEAT_MIN = 62
SETPOINT_HEAT_MAX = 78

def set_nest_temp(config: dict, device_id: str, target_f: float, zone_label: str, hvac_mode: str = "") -> bool:
    """
    Push a new setpoint to a Nest thermostat — Steve's canon season-based logic.
    SetCool in summer, SetHeat in winter, SetRange in shoulder months.
    User is expected to manually keep the thermostat in COOL or HEAT mode
    matching the SRP season.

    Setpoints are clamped (Option C) to keep Nest from auto-flipping to ECO
    when the agent commands a far-from-room-temp setpoint:
      cool:  65–80°F   (caps Critical tier at 80°F instead of canon's 82°F)
      heat:  62–78°F
    """
    try:
        creds    = get_nest_credentials(config)
        headers  = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type":  "application/json",
        }
        season    = get_season(datetime.now(TZ).month)
        # Clamp by season (matches the command we're about to send)
        if season in ("SUMMER", "SUMMER_PEAK"):
            clamped_f = max(SETPOINT_COOL_MIN, min(SETPOINT_COOL_MAX, target_f))
        elif season == "WINTER":
            clamped_f = max(SETPOINT_HEAT_MIN, min(SETPOINT_HEAT_MAX, target_f))
        else:
            clamped_f = max(SETPOINT_HEAT_MIN, min(SETPOINT_COOL_MAX, target_f))
        if clamped_f != target_f:
            log.info(f"Nest [{zone_label}] target {target_f}°F clamped to {clamped_f}°F")
            target_f = clamped_f
        target_c  = round((target_f - 32) * 5 / 9, 1)

        if season in ("SUMMER", "SUMMER_PEAK"):
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
            body    = {"params": {"coolCelsius": target_c}}
        elif season == "WINTER":
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
            body    = {"params": {"heatCelsius": target_c}}
        else:
            command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange"
            body    = {"params": {"heatCelsius": target_c - 1, "coolCelsius": target_c + 1}}

        body["command"] = command
        project = config["nest_project_id"]
        url     = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}:executeCommand"
        resp    = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        log.info(f"Nest [{zone_label}] → {target_f}°F ({target_c}°C) via {command.split('.')[-1]}")
        return True
    except Exception as e:
        log.error(f"Nest SET error [{zone_label}]: {e}")
        return False

def detect_manual_override(nest_state: dict, last_agent_targets: dict) -> dict[str, bool]:
    """
    Compare current Nest setpoints against what the agent last commanded.
    Returns {zone_id: True} if a manual override is detected.
    """
    overrides = {}
    for zone_id, state in nest_state.items():
        last_target = last_agent_targets.get(zone_id)
        if last_target is not None:
            current_set = state.get("set_temp_f", last_target)
            # Consider it overridden if the setpoint differs by more than 0.5°F
            if abs(current_set - last_target) > 0.5:
                overrides[zone_id] = True
                log.info(f"Manual override detected [{zone_id}]: set {current_set}°F vs agent {last_target}°F")
    return overrides

# ═══════════════════════════════════════════════════════════════════════════
# WEATHER — Open-Meteo (free, no API key, covers Mesa AZ)
# ═══════════════════════════════════════════════════════════════════════════

def get_forecast_temp(lat: float = 33.4152, lon: float = -111.8315) -> float | None:
    """
    Fetch tomorrow's forecasted high temperature (°F) for Mesa AZ.
    Uses Open-Meteo free API — no key required.
    """
    try:
        url    = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude":             lat,
            "longitude":            lon,
            "daily":                "temperature_2m_max",
            "temperature_unit":     "fahrenheit",
            "timezone":             "America/Phoenix",
            "forecast_days":        2,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Index 0 = today, index 1 = tomorrow
        tomorrow_high = data["daily"]["temperature_2m_max"][1]
        log.info(f"Tomorrow forecast high: {tomorrow_high}°F")
        return tomorrow_high
    except Exception as e:
        log.error(f"Weather API error: {e}")
        return None

def get_current_temp(lat: float = 33.4152, lon: float = -111.8315) -> float | None:
    """Fetch current outside temperature for Mesa AZ."""
    try:
        url    = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude":         lat,
            "longitude":        lon,
            "current_weather":  True,
            "temperature_unit": "fahrenheit",
            "timezone":         "America/Phoenix",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        temp = resp.json()["current_weather"]["temperature"]
        log.info(f"Current outside temp: {temp}°F")
        return temp
    except Exception as e:
        log.error(f"Current weather error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# LEARNING DATA RECORDING
# ═══════════════════════════════════════════════════════════════════════════

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []

def save_history(history: list[dict]) -> None:
    # Keep last 60 days
    HISTORY_FILE.write_text(json.dumps(history[-60:], indent=2))

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))

def update_daily_record(state: dict, pwall: dict, dt: datetime) -> dict:
    """
    Update the in-progress day record with peak entry/exit battery data
    and accumulated solar during peak.
    """
    dr     = state.get("day_record", {})
    season = get_season(dt.month)
    in_pk  = is_peak(dt)
    batt   = pwall.get("battery_pct")
    sol_kw = pwall.get("solar_kw", 0)

    # Track daily high temp
    outside_f = state.get("outside_temp_f", 70)
    dr["day_high_f"]  = max(dr.get("day_high_f", 0), outside_f)
    dr["season"]      = season

    # Peak entry
    was_in_peak = state.get("was_in_peak", False)
    if in_pk and not was_in_peak and batt is not None:
        dr["batt_at_peak_start"] = batt
        dr["solar_kwh_peak"]     = 0.0
        dr["solar_kwh_phase1"]   = 0.0
        log.info(f"Peak entry recorded: battery {batt:.0f}%")

    # Accumulate solar during peak (every tick ≈ poll_interval / 60 hours)
    if in_pk and batt is not None:
        step_hrs = state.get("poll_interval_mins", 15) / 60
        dr["solar_kwh_peak"]  = dr.get("solar_kwh_peak", 0) + sol_kw * step_hrs
        if dt.hour < 17:   # phase 1 = 2–5pm
            dr["solar_kwh_phase1"] = dr.get("solar_kwh_phase1", 0) + sol_kw * step_hrs

    # Peak exit — compute depletion
    if not in_pk and was_in_peak and batt is not None:
        start = dr.get("batt_at_peak_start")
        if start is not None:
            gross = max(0, start - batt)
            gross_kwh = round(gross / 100 * BATTERY_KWH, 2)
            solar_kwh = round(dr.get("solar_kwh_peak", 0), 2)
            net_kwh   = round(max(0, gross_kwh - solar_kwh), 2)
            theor     = theoretical_peak_solar_kwh(outside_f)
            cloud     = round(min(1, solar_kwh / theor), 3)
            coverage  = round(solar_kwh / gross_kwh * 100, 1) if gross_kwh > 0 else 0
            dr.update({
                "batt_at_peak_end":    batt,
                "gross_kwh_depleted":  gross_kwh,
                "solar_kwh_peak":      solar_kwh,
                "solar_kwh_phase1":    round(dr.get("solar_kwh_phase1", 0), 2),
                "net_kwh_depleted":    net_kwh,
                "cloud_factor":        cloud,
                "peak_solar_coverage": coverage,
            })
            log.info(f"Peak exit: gross {gross_kwh}kWh, solar {solar_kwh}kWh, net {net_kwh}kWh, cloud {cloud:.0%}")

    state["day_record"]  = dr
    state["was_in_peak"] = in_pk
    return state

def maybe_commit_day(state: dict, history: list[dict], dt: datetime) -> tuple[dict, list[dict]]:
    """Commit yesterday's day_record on the first cycle of any new calendar day.

    Cadence-tolerant: works regardless of scheduler clock alignment, restart
    timing, or missed midnight wake-ups. The scheduler runs at HH:20:42 and
    HH:50:42, so the prior wall-clock window (`hour==0 and minute<20`) was
    unreachable.
    """
    last_commit = state.get("last_day_commit", "")
    today_str   = dt.strftime("%Y-%m-%d")
    prev_str    = (dt - timedelta(days=1)).strftime("%Y-%m-%d")

    if last_commit != today_str:
        dr = state.get("day_record", {})
        if dr.get("net_kwh_depleted") is not None:
            dr["date"] = prev_str
            history.append(dr)
            save_history(history)
            log.info(f"Day record committed: {prev_str} | {dr}")
        state["last_day_commit"] = today_str
        state["day_record"]      = {"day_high_f": 0}  # reset for new day

    return state, history

# ═══════════════════════════════════════════════════════════════════════════
# MAIN CYCLE
# ═══════════════════════════════════════════════════════════════════════════

LOCK_FILE = BASE_DIR / "agent.lock"

def run_cycle(config: dict) -> None:
    """One complete agent cycle: read → decide → act → log."""
    if LOCK_FILE.exists():
        log.warning("Lock file exists — previous cycle still running, skipping")
        return
    LOCK_FILE.touch()
    try:
        _run_cycle(config)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def _run_cycle(config: dict) -> None:
    now     = datetime.now(TZ)
    log.info(f"=== Agent cycle {now.strftime('%Y-%m-%d %H:%M')} ===")

    # ── Load persistent state ────────────────────────────────────────────────
    state   = load_state()
    history = load_history()

    # ── Authenticate ─────────────────────────────────────────────────────────
    tesla = TeslaFleet(
        client_id=config["tesla_client_id"],
        client_secret=config["tesla_client_secret"],
        redirect_uri=config["tesla_redirect_uri"],
        tokens_file=str(BASE_DIR / "data" / "tesla_tokens.json"),
    )
    if not tesla.authorized:
        log.error("Tesla Fleet API not authorized — visit /oauth/tesla/login. Skipping Tesla actions this cycle.")
        # Continue with Nest only — agent decisions degrade gracefully
        tesla = None

    # ── Read sensors ─────────────────────────────────────────────────────────
    lat = config.get("latitude", 33.4152)
    lon = config.get("longitude", -111.8315)
    pwall     = get_powerwall_state(tesla, config)
    nest_devs = get_nest_state(config)
    outside_f = get_current_temp(lat, lon) or state.get("outside_temp_f", 90)
    forecast_f = get_forecast_temp(lat, lon) or state.get("forecast_temp_f", outside_f)

    state["outside_temp_f"]  = outside_f
    state["forecast_temp_f"] = forecast_f

    if not pwall:
        log.warning("Powerwall data unavailable — skipping cycle")
        return

    # ── Detect manual overrides from Nest ────────────────────────────────────
    last_targets    = state.get("last_agent_targets", {})
    overrides       = detect_manual_override(nest_devs, last_targets)
    hold_state      = state.get("hold_state", {
        "family": {"type": "agent", "held_temp": comfort_temp(now)},
        "guest":  {"type": "agent", "held_temp": comfort_temp(now)},
    })

    # Day→Night (9pm) and Night→Day (5am) resets
    prev_hour = state.get("prev_hour", now.hour)
    if (prev_hour < 21 and now.hour >= 21) or ((prev_hour >= 21 or prev_hour < 5) and now.hour >= 5 and now.hour < 21):
        log.info("Day/Night boundary — releasing all manual holds")
        hold_state = {
            z: {"type": "agent", "held_temp": comfort_temp(now)}
            for z in ("family", "guest")
        }

    # Apply detected overrides as guest holds
    for zone_id, is_overridden in overrides.items():
        if is_overridden and zone_id in nest_devs:
            held_temp = nest_devs[zone_id]["set_temp_f"]
            hold_state[zone_id] = {
                "type":      "guest",
                "held_temp": held_temp,
                "set_at":    now.strftime("%H:%M"),
                "note":      f"Auto-detected override → {held_temp}°F",
            }
            log.info(f"Auto-hold applied [{zone_id}]: {held_temp}°F")

    state["hold_state"] = hold_state
    state["prev_hour"]  = now.hour

    # ── Merge sensor data into state ─────────────────────────────────────────
    state.update({
        "battery_pct": pwall["battery_pct"],
        "solar_kw":    pwall["solar_kw"],
        "load_kw":     pwall.get("load_kw", 0),
        "grid_kw":     pwall.get("grid_kw", 0),
    })

    # ── Update learning day record ────────────────────────────────────────────
    state   = update_daily_record(state, pwall, now)
    state, history = maybe_commit_day(state, history, now)

    # ── Run agent decision engine ─────────────────────────────────────────────
    decision = run_agent(state, config, history)

    for msg in decision["decisions"]:
        log.info(f"  {msg}")

    log.info(f"  Lever: {decision['lever']} | Grid: {decision['grid_allowed']} | "
             f"targetPrePeak: {decision['target_pre_peak']}% "
             f"(base {decision['base_target']}% + hold adj {decision['hold_adj']}%)")

    # ── Act: set Powerwall mode ───────────────────────────────────────────────
    set_powerwall_mode(
        tesla, config,
        grid_allowed=decision["grid_allowed"],
        battery_pct=pwall["battery_pct"],
        target_reserve=decision["target_reserve"],
    )

    # ── Act: push thermostat commands ─────────────────────────────────────────
    for zone_id, target_f in decision["thermostat_targets"].items():
        if zone_id in nest_devs:
            if nest_devs[zone_id].get("eco_active"):
                log.info(f"Nest [{zone_id}] in ECO mode — agent stays out (user override)")
                continue
            device_id = nest_devs[zone_id]["device_id"]
            current   = nest_devs[zone_id]["set_temp_f"]
            # Only push if the target differs from current by more than 0.5°F
            if abs(target_f - current) > 0.5:
                # Stagger Family Room and Guest Wing by 3 min to avoid simultaneous
                # compressor startups (demand charge spike prevention)
                if zone_id == "guest":
                    time.sleep(180)
                set_nest_temp(config, device_id, target_f, zone_id)
            else:
                log.info(f"Nest [{zone_id}] already at {current}°F — no command needed")
        else:
            log.warning(f"Nest zone [{zone_id}] not found in device list")

    # ── Persist state ─────────────────────────────────────────────────────────
    state["last_agent_targets"] = decision["thermostat_targets"]
    state["last_decision"]      = decision
    state["poll_interval_mins"] = decision["poll_interval_mins"]
    save_state(state)

    log.info(f"=== Cycle complete — next poll in {decision['poll_interval_mins']} min ===\n")

# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def main():
    if not CONFIG_FILE.exists():
        log.error(f"config.json not found at {CONFIG_FILE}")
        log.error("Copy config.example.json → config.json and fill in your credentials")
        return

    config = json.loads(CONFIG_FILE.read_text())
    log.info("SRP Energy Agent starting")
    log.info(f"Tesla site: {config.get('tesla_site_id', 'auto-detect')}")
    log.info(f"Nest project: {config.get('nest_project_id', '?')}")

    run_cycle(config)

if __name__ == "__main__":
    main()
