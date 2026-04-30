"""Tesla Fleet API client — drop-in replacement for the retired teslapy ownerapi flow."""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

FLEET_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
AUTH_URL   = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL  = "https://auth.tesla.com/oauth2/v3/token"
SCOPES     = "openid offline_access energy_device_data energy_cmds"

ENDPOINTS = {
    "PRODUCT_LIST":            ("GET",  "/api/1/products"),
    "SITE_DATA":               ("GET",  "/api/1/energy_sites/{site_id}/live_status"),
    "BATTERY_BACKUP_RESERVE":  ("POST", "/api/1/energy_sites/{site_id}/backup"),
    "BATTERY_OPERATION_MODE":  ("POST", "/api/1/energy_sites/{site_id}/operation"),
}


class TeslaFleet:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, tokens_file: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self.redirect_uri  = redirect_uri
        self.tokens_file   = Path(tokens_file)
        self._tokens: Optional[dict] = None
        self._load_tokens()

    def _load_tokens(self):
        if self.tokens_file.exists():
            self._tokens = json.loads(self.tokens_file.read_text())

    def _save_tokens(self, tokens: dict):
        tokens["obtained_at"] = int(time.time())
        self._tokens = tokens
        self.tokens_file.write_text(json.dumps(tokens, indent=2))
        self.tokens_file.chmod(0o600)

    @property
    def authorized(self) -> bool:
        return self._tokens is not None and "access_token" in self._tokens

    def authorize_url(self, state: str) -> str:
        from urllib.parse import urlencode
        params = {
            "response_type": "code",
            "client_id":     self.client_id,
            "redirect_uri":  self.redirect_uri,
            "scope":         SCOPES,
            "state":         state,
            "prompt":        "login",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str):
        r = httpx.post(TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "code":          code,
            "redirect_uri":  self.redirect_uri,
            "audience":      FLEET_BASE,
        }, timeout=30)
        r.raise_for_status()
        self._save_tokens(r.json())

    def _refresh(self):
        if not self._tokens or "refresh_token" not in self._tokens:
            raise RuntimeError("No refresh token available — re-authorize via /oauth/tesla/login")
        r = httpx.post(TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "client_id":     self.client_id,
            "refresh_token": self._tokens["refresh_token"],
        }, timeout=30)
        r.raise_for_status()
        new = r.json()
        new.setdefault("refresh_token", self._tokens["refresh_token"])
        self._save_tokens(new)

    def _ensure_fresh(self):
        if not self._tokens:
            raise RuntimeError("Not authorized — visit /oauth/tesla/login")
        age = int(time.time()) - self._tokens.get("obtained_at", 0)
        if age >= self._tokens.get("expires_in", 28800) - 300:
            log.info("Tesla access token near expiry — refreshing")
            self._refresh()

    def fetch_token(self):
        if not self.authorized:
            raise RuntimeError(
                "Tesla Fleet API not authorized. Visit "
                "https://srp.hollandit.work/oauth/tesla/login to complete OAuth."
            )

    def api(self, name: str, path_vars: Optional[dict] = None, **kwargs):
        if name not in ENDPOINTS:
            raise ValueError(f"Unknown endpoint: {name}")
        self._ensure_fresh()
        method, path = ENDPOINTS[name]
        if path_vars:
            path = path.format(**path_vars)
        url = FLEET_BASE + path
        headers = {"Authorization": f"Bearer {self._tokens['access_token']}"}

        if method == "GET":
            r = httpx.get(url, headers=headers, timeout=30)
        else:
            r = httpx.post(url, headers=headers, json=kwargs, timeout=30)
        if r.status_code == 401:
            log.warning("401 from Fleet API — refreshing and retrying")
            self._refresh()
            headers["Authorization"] = f"Bearer {self._tokens['access_token']}"
            if method == "GET":
                r = httpx.get(url, headers=headers, timeout=30)
            else:
                r = httpx.post(url, headers=headers, json=kwargs, timeout=30)
        r.raise_for_status()
        return r.json()


def register_partner(client_id: str, client_secret: str, domain: str) -> dict:
    """One-time partner registration. Tesla fetches the public key from the domain to verify."""
    r = httpx.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "openid energy_device_data energy_cmds",
        "audience":      FLEET_BASE,
    }, timeout=30)
    r.raise_for_status()
    partner_token = r.json()["access_token"]

    r = httpx.post(
        f"{FLEET_BASE}/api/1/partner_accounts",
        headers={"Authorization": f"Bearer {partner_token}"},
        json={"domain": domain},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
