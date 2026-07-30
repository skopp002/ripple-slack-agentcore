"""Ripple CLI login — Auth0 Device Authorization flow.

Produces a REAL user access token (JWT) for the signed-in person. This is the
demo stand-in for the production path (Auth0 SSO -> Slack -> Receiver Lambda);
the token it yields is exactly what the ingress gateway would receive.

Usage:
    python client/login.py                 # interactive: prints URL+code, polls
    python client/login.py --print-token   # also print the raw access token
The token is cached at client/.token.json and reused until it expires.
"""
import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

import jwt  # PyJWT
import requests

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from env_util import require_env  # noqa: E402

DOMAIN = require_env("AUTH0_DOMAIN")
CLIENT_ID = require_env("AUTH0_CLI_CLIENT_ID")
AUDIENCE = require_env("AUTH0_AUDIENCE")
SCOPE = require_env("AUTH0_SCOPE")

TOKEN_CACHE = Path(__file__).with_name(".token.json")


def _device_code():
    r = requests.post(
        f"https://{DOMAIN}/oauth/device/code",
        data={"client_id": CLIENT_ID, "audience": AUDIENCE, "scope": SCOPE},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _poll_for_token(device_code, interval):
    while True:
        time.sleep(interval)
        r = requests.post(
            f"https://{DOMAIN}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": CLIENT_ID,
            },
            timeout=15,
        )
        data = r.json()
        if r.status_code == 200:
            return data
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 2
            continue
        raise RuntimeError(f"device flow failed: {data}")


def _cached_token():
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        tok = data["access_token"]
        claims = jwt.decode(tok, options={"verify_signature": False})
        if claims.get("exp", 0) - time.time() > 60:
            return data
    except Exception:
        return None
    return None


def login(force=False):
    """Return a token dict {access_token, id_token?, refresh_token?, ...}."""
    if not force:
        cached = _cached_token()
        if cached:
            return cached

    dc = _device_code()
    url = dc["verification_uri_complete"]
    print("\n=== Ripple login ===")
    print(f"1. Open: {url}")
    print(f"   (or {dc['verification_uri']} and enter code {dc['user_code']})")
    print("2. Sign in and approve.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("Waiting for you to complete login in the browser...")
    tok = _poll_for_token(dc["device_code"], dc.get("interval", 5))
    TOKEN_CACHE.write_text(json.dumps(tok))
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    print(f"\n✅ Logged in as: {claims.get('sub')}  (email: {claims.get('email','n/a')})")
    return tok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache, re-login")
    ap.add_argument("--print-token", action="store_true")
    ap.add_argument("--claims", action="store_true", help="print decoded claims")
    args = ap.parse_args()
    tok = login(force=args.force)
    if args.print_token:
        print(tok["access_token"])
    if args.claims:
        claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
        print(json.dumps(claims, indent=2))
