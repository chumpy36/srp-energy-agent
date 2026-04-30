#!/usr/bin/env python3
"""
SRP Energy Agent — Web Dashboard
FastAPI app + APScheduler (replaces cron in Docker).
"""
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent import CONFIG_FILE, TZ, load_history, load_state, run_cycle, save_state
from tesla_fleet import TeslaFleet
from google_auth_oauthlib.flow import Flow as GoogleFlow

NEST_SCOPES = ["https://www.googleapis.com/auth/sdm.service"]

log = logging.getLogger("srp-web")

BASE_DIR  = Path(__file__).parent
LOG_FILE  = BASE_DIR / "agent.log"

app       = FastAPI(title="SRP Energy Agent")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_config: dict = {}
_scheduler    = BackgroundScheduler(timezone="America/Phoenix")


def _agent_job() -> None:
    try:
        run_cycle(_config)
    except Exception as e:
        log.error(f"Scheduler agent error: {e}", exc_info=True)


@app.on_event("startup")
def startup() -> None:
    global _config
    if not CONFIG_FILE.exists():
        log.error("config.json not found — dashboard starting without agent scheduler")
        return
    _config = json.loads(CONFIG_FILE.read_text())
    poll_min = int(_config.get("poll_interval_mins", 30))
    _scheduler.add_job(
        _agent_job, "interval", minutes=poll_min, id="agent",
        max_instances=1, next_run_time=datetime.now(TZ),
    )
    _scheduler.start()
    log.info(f"APScheduler started — agent runs every {poll_min} minutes")


@app.on_event("shutdown")
def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state   = load_state()
    history = load_history()
    state["poll_interval_mins"] = int(_config.get("poll_interval_mins", 30))
    log_lines: list[str] = []
    if LOG_FILE.exists():
        log_lines = LOG_FILE.read_text().splitlines()[-100:]
    return templates.TemplateResponse("index.html", {
        "request":    request,
        "state":      state,
        "history":    list(reversed(history[-30:])),
        "log_lines":  list(reversed(log_lines)),
        "user_email": request.headers.get("CF-Access-Authenticated-User-Email", ""),
        "now":        datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/state")
def api_state() -> dict:
    return load_state()


@app.get("/api/log")
def api_log(n: int = 50) -> list[str]:
    if not LOG_FILE.exists():
        return []
    return list(reversed(LOG_FILE.read_text().splitlines()[-n:]))


_oauth_states: dict[str, float] = {}


def _tesla_client() -> TeslaFleet:
    return TeslaFleet(
        client_id=_config["tesla_client_id"],
        client_secret=_config["tesla_client_secret"],
        redirect_uri=_config["tesla_redirect_uri"],
        tokens_file=str(BASE_DIR / "data" / "tesla_tokens.json"),
    )


@app.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
def tesla_public_key() -> PlainTextResponse:
    pem = (BASE_DIR / "data" / "tesla_public_key.pem").read_text()
    return PlainTextResponse(pem, media_type="application/x-pem-file")


@app.get("/oauth/tesla/login")
def tesla_login() -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = datetime.now().timestamp()
    return RedirectResponse(_tesla_client().authorize_url(state))


@app.get("/oauth/tesla/callback", response_class=HTMLResponse)
def tesla_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    if error:
        return HTMLResponse(f"<h1>Tesla OAuth error</h1><pre>{error}</pre>", status_code=400)
    if state not in _oauth_states:
        return HTMLResponse("<h1>Invalid OAuth state</h1>", status_code=400)
    _oauth_states.pop(state, None)
    try:
        _tesla_client().exchange_code(code)
    except Exception as e:
        log.error(f"Tesla token exchange failed: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{e}</pre>", status_code=500)
    return HTMLResponse(
        "<h1>Tesla authorized</h1>"
        "<p>Tokens saved. The agent will pick them up on the next cycle.</p>"
        "<p><a href='/'>Back to dashboard</a></p>"
    )


def _nest_redirect_uri() -> str:
    return _config.get("nest_redirect_uri", "https://srp.hollandit.work/oauth/nest/callback")


def _nest_flow(state: str | None = None) -> GoogleFlow:
    secrets_path = BASE_DIR / "data" / _config.get("nest_client_secrets_file", "client_secrets.json")
    flow = GoogleFlow.from_client_secrets_file(str(secrets_path), scopes=NEST_SCOPES, state=state)
    flow.redirect_uri = _nest_redirect_uri()
    return flow


@app.get("/oauth/nest/login")
def nest_login() -> RedirectResponse:
    project_id = _config["nest_project_id"]
    flow = _nest_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        # SDM requires a partner-specific authorization endpoint:
    )
    sdm_url = (
        f"https://nestservices.google.com/partnerconnections/{project_id}/auth"
        f"?redirect_uri={_nest_redirect_uri()}"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&client_id={flow.client_config['client_id']}"
        f"&response_type=code"
        f"&scope={'+'.join(NEST_SCOPES)}"
        f"&state={state}"
    )
    _oauth_states[state] = datetime.now().timestamp()
    return RedirectResponse(sdm_url)


@app.get("/oauth/nest/callback", response_class=HTMLResponse)
def nest_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    if error:
        return HTMLResponse(f"<h1>Nest OAuth error</h1><pre>{error}</pre>", status_code=400)
    if state not in _oauth_states:
        return HTMLResponse("<h1>Invalid OAuth state</h1>", status_code=400)
    _oauth_states.pop(state, None)
    try:
        flow = _nest_flow(state=state)
        flow.fetch_token(code=code)
        token_path = BASE_DIR / "data" / "nest_token.json"
        token_path.write_text(flow.credentials.to_json())
        token_path.chmod(0o600)
    except Exception as e:
        log.error(f"Nest token exchange failed: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{e}</pre>", status_code=500)
    return HTMLResponse(
        "<h1>Nest authorized</h1>"
        "<p>Tokens saved. The agent will pick them up on the next cycle.</p>"
        "<p><a href='/'>Back to dashboard</a></p>"
    )


@app.get("/api/poll-interval")
def get_poll_interval() -> dict:
    return {"minutes": int(_config.get("poll_interval_mins", 30))}


@app.post("/api/poll-interval")
async def set_poll_interval(request: Request) -> dict:
    body = await request.json()
    minutes = body.get("minutes")
    if not isinstance(minutes, (int, float)) or not (5 <= int(minutes) <= 240):
        raise HTTPException(400, "minutes must be an integer between 5 and 240")
    minutes = int(minutes)
    _config["poll_interval_mins"] = minutes
    CONFIG_FILE.write_text(json.dumps(_config, indent=2))
    _scheduler.reschedule_job("agent", trigger="interval", minutes=minutes)
    user = request.headers.get("CF-Access-Authenticated-User-Email", "unknown")
    log.info(f"Poll interval changed to {minutes} min by {user}")
    return {"ok": True, "minutes": minutes}


@app.post("/api/override/{zone}")
async def set_override(zone: str, request: Request) -> dict:
    """Set a manual thermostat hold. Body: {"temp_f": 74}"""
    if zone not in ("family", "guest"):
        raise HTTPException(400, "Zone must be 'family' or 'guest'")
    body   = await request.json()
    temp_f = body.get("temp_f")
    if not isinstance(temp_f, (int, float)) or not (60 <= float(temp_f) <= 90):
        raise HTTPException(400, "temp_f must be between 60 and 90")
    user  = request.headers.get("CF-Access-Authenticated-User-Email", "unknown")
    state = load_state()
    hold  = state.get("hold_state", {})
    hold[zone] = {
        "type":      "guest",
        "held_temp": float(temp_f),
        "set_at":    datetime.now(TZ).strftime("%H:%M"),
        "note":      f"Manual override by {user}",
    }
    state["hold_state"] = hold
    save_state(state)
    log.info(f"Manual override [{zone}] → {temp_f}°F by {user}")
    return {"ok": True, "zone": zone, "temp_f": float(temp_f)}
