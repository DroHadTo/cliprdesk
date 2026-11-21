"""Whop's account and user API connections for the local Clip Desk."""
import base64
from contextlib import closing
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
import secret_store

import cliptool as ct
import posts


ORIGIN = "https://api.whop.com"
VERSION = "2026-11-20"
SCOPES = "openid profile bounty:basic:read bounty:submission:create"
LOCK = threading.RLock()
PENDING = {}


class WhopError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def callback_url():
    return f"http://localhost:{ct.PORT}/whop/oauth/callback"


def valid_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(rf"{prefix}_[A-Za-z0-9]+", value) is not None


def oauth_configured():
    return bool(os.getenv("WHOP_CLIENT_ID", "").strip() and secret_store.value("WHOP_CLIENT_SECRET").strip())


def response_json(response, expected):
    if response.status_code != expected:
        hints = {401: "Sign in again or check your access key.", 403: "Whop declined the requested scope.",
                 429: "Whop rate limited the request."}
        extra = hints.get(response.status_code, "")
        raise WhopError(f"Whop returned HTTP {response.status_code}. {extra}".strip(), response.status_code)
    payload = response.json()
    if not isinstance(payload, dict):
        raise WhopError("Whop returned an unexpected response.")
    return payload


def call(path, token, *, params=None, body=None, key=None):
    headers = {"Authorization": f"Bearer {token}", "Api-Version-Date": VERSION}
    if key:
        headers["Idempotency-Key"] = key
    method = "GET" if body is None else "POST"
    response = requests.request(method, f"{ORIGIN}/api/v1/{path}", headers=headers, params=params,
                                json=body, timeout=(6, 24), allow_redirects=False)
    return response_json(response, 200 if method == "GET" else 201)


def begin():
    if not oauth_configured():
        raise WhopError("Save the Whop OAuth client ID and secret first.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    client = os.environ["WHOP_CLIENT_ID"].strip()
    with LOCK:
        PENDING.clear()
        PENDING[state] = (client, verifier, time.time() + 600)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = f"{ORIGIN}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client, "redirect_uri": callback_url(),
        "scope": SCOPES, "state": state, "nonce": secrets.token_urlsafe(24),
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return url, state


def token_exchange(params):
    response = requests.post(f"{ORIGIN}/oauth/token", json=params | {
        "client_id": os.environ["WHOP_CLIENT_ID"].strip(), "client_secret": secret_store.value("WHOP_CLIENT_SECRET").strip(),
    }, timeout=(6, 24), allow_redirects=False)
    return response_json(response, 200)


def save_tokens(payload, client, worker, previous_refresh=""):
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or previous_refresh
    lifetime = payload.get("expires_in")
    scope = payload.get("scope")
    if not isinstance(access, str) or not access or type(lifetime) is not int or lifetime <= 0:
        raise WhopError("Whop did not provide a usable user access token.")
    if scope is not None and (not isinstance(scope, str) or "bounty:submission:create" not in scope.split()):
        raise WhopError("Whop has not granted bounty submission access to this app.")
    try:
        saved_access, saved_refresh = secret_store.protect(access), secret_store.protect(refresh)
    except secret_store.SecretError as exc:
        raise WhopError("Windows could not protect the Whop connection. Reconnect from your Windows account.") from exc
    with posts.transaction() as conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("INSERT OR REPLACE INTO whop_oauth VALUES(1,?,?,?,?,?)",
                     (client, worker, saved_access, saved_refresh, time.time() + lifetime))
    return worker, access


def finish(code, state, cookie):
    if not code or not state or not hmac.compare_digest(state.encode(), (cookie or "").encode()):
        raise WhopError("The Whop sign-in could not be verified. Start it again.")
    with LOCK:
        pending = PENDING.pop(state, None)
        if not pending or not oauth_configured() or pending[0] != os.getenv("WHOP_CLIENT_ID", "").strip() or pending[2] <= time.time():
            raise WhopError("The Whop sign-in expired. Start it again.")
        client, verifier, _ = pending
        token = token_exchange({"grant_type": "authorization_code", "code": code,
                                "redirect_uri": callback_url(), "code_verifier": verifier})
        response = requests.get(f"{ORIGIN}/oauth/userinfo", headers={"Authorization": f"Bearer {token['access_token']}"},
                                timeout=(6, 24), allow_redirects=False)
        profile = response_json(response, 200)
        worker = profile.get("sub")
        if not valid_id(worker, "user"):
            raise WhopError("Whop did not return a valid user ID.")
        save_tokens(token, client, worker)


def connected_worker():
    with closing(ct.db()) as conn:
        row = conn.execute("SELECT worker,client FROM whop_oauth WHERE id=1").fetchone()
    return row["worker"] if row and row["client"] == os.getenv("WHOP_CLIENT_ID", "").strip() else ""


def migrate_tokens():
    """Replace legacy plaintext under the same transaction as the database update."""
    with LOCK, posts.transaction() as conn:
        row = conn.execute("SELECT access, refresh FROM whop_oauth WHERE id=1").fetchone()
        if row is None:
            return
        access, refresh = row["access"], row["refresh"]
        if access.startswith(secret_store.PREFIX) and (not refresh or refresh.startswith(secret_store.PREFIX)):
            return
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("UPDATE whop_oauth SET access=?,refresh=? WHERE id=1",
                     (access if access.startswith(secret_store.PREFIX) else secret_store.protect(access),
                      refresh if refresh.startswith(secret_store.PREFIX) else secret_store.protect(refresh)))


def user_token():
    if not oauth_configured():
        return None
    with LOCK:
        client = os.environ["WHOP_CLIENT_ID"].strip()
        try:
            migrate_tokens()
        except secret_store.SecretError as exc:
            raise WhopError("Windows could not migrate the Whop sign-in. Reconnect in Setup.") from exc
        with closing(ct.db()) as conn:
            row = conn.execute("SELECT * FROM whop_oauth WHERE id=1 AND client=?", (client,)).fetchone()
        if not row:
            return None
        try:
            if row["expires"] > time.time() + 60:
                return row["worker"], secret_store.unprotect(row["access"])
            refresh = secret_store.unprotect(row["refresh"])
        except secret_store.SecretError as exc:
            raise WhopError("Windows could not open the Whop sign-in. Reconnect in Setup.") from exc
        if not refresh:
            raise WhopError("Whop sign-in expired. Reconnect your user account.")
        tokens = token_exchange({"grant_type": "refresh_token", "refresh_token": refresh})
        return save_tokens(tokens, client, row["worker"], refresh)


def disconnect():
    with LOCK, posts.transaction() as conn:
        PENDING.clear()
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("DELETE FROM whop_oauth WHERE id=1")
