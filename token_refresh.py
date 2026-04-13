#!/usr/bin/env python3
"""
Bullpen Token Auto-Refresh
--------------------------
Hits the Bullpen usergate API directly with the stored refresh token
to get a fresh access token — bypassing the broken CLI refresh.

Run standalone:   python3 token_refresh.py
Or import:        from token_refresh import refresh_token_if_needed
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

CREDS_FILE   = os.path.expanduser("~/.bullpen/credentials.json")
USERGATE_URL = "https://usergate.bullpen.fi"

# All cookie / header names the Bullpen backend might use for the refresh token
COOKIE_NAMES_TO_TRY = [
    "refresh_token",
    "refreshToken",
    "rt",
    "token",
    "auth_token",
    "session",
    "__Secure-refresh_token",
]

REFRESH_PATHS = [
    "/api/auth/refresh",
    "/api/auth/token/refresh",
    "/api/v1/auth/refresh",
    "/auth/refresh",
]


def load_credentials() -> dict:
    if not os.path.exists(CREDS_FILE):
        print(f"[refresh] ERROR: credentials file not found: {CREDS_FILE}")
        return {}
    with open(CREDS_FILE) as f:
        return json.load(f)


def save_credentials(creds: dict) -> None:
    with open(CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    print("[refresh] Credentials updated.")


def _post(url: str, headers: dict, body: bytes) -> tuple:
    """Returns (status_code, response_body_str).  Never raises."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode()
        except Exception:
            return e.code, ""
    except Exception as ex:
        return 0, str(ex)


def _try_cookie(base_url: str, path: str, cookie_name: str, token: str) -> dict | None:
    """Try sending the refresh token as an HTTP cookie.  Returns new tokens or None."""
    url     = base_url.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "Cookie":       f"{cookie_name}={token}",
        "Accept":       "application/json",
    }
    status, body = _post(url, headers, b"{}")
    print(f"[refresh]   Cookie {cookie_name!r} on {path} → {status}: {body[:120]}")
    if status == 200:
        try:
            return json.loads(body)
        except Exception:
            pass
    return None


def _try_bearer(base_url: str, path: str, token: str) -> dict | None:
    """Try sending the refresh token as Bearer auth in the header."""
    url     = base_url.rstrip("/") + path
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }
    status, body = _post(url, headers, b"{}")
    print(f"[refresh]   Bearer on {path} → {status}: {body[:120]}")
    if status == 200:
        try:
            return json.loads(body)
        except Exception:
            pass
    return None


def _try_body(base_url: str, path: str, token: str) -> dict | None:
    """Try sending the refresh token in the JSON body."""
    url     = base_url.rstrip("/") + path
    payload = json.dumps({"refresh_token": token, "refreshToken": token}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }
    status, body = _post(url, headers, payload)
    print(f"[refresh]   Body on {path} → {status}: {body[:120]}")
    if status == 200:
        try:
            return json.loads(body)
        except Exception:
            pass
    return None


def _extract_access_token(response: dict) -> str:
    """Pull the new access token out of whichever field the API uses."""
    for key in ("access_token", "accessToken", "token", "jwt", "id_token"):
        val = response.get(key)
        if val and isinstance(val, str) and len(val) > 20:
            return val
    # Check nested structures
    for key in ("data", "result", "auth"):
        sub = response.get(key)
        if isinstance(sub, dict):
            for k in ("access_token", "accessToken", "token"):
                val = sub.get(k)
                if val and isinstance(val, str) and len(val) > 20:
                    return val
    return ""


def refresh_token_if_needed(force: bool = False) -> bool:
    """
    Refresh the access token if it's expiring within 5 minutes, or if force=True.
    Returns True on success, False on failure.
    """
    creds = load_credentials()
    if not creds:
        return False

    refresh_token = creds.get("refresh_token", "")
    if not refresh_token:
        print("[refresh] No refresh_token in credentials — cannot refresh.")
        return False

    # Check if refresh is actually needed
    expiry = creds.get("session_expiration", 0)
    if not force and expiry > time.time() + 300:
        # Still valid for more than 5 minutes
        return True

    usergate = creds.get("usergate_url", USERGATE_URL)
    print(f"[refresh] Access token expiring soon. Attempting refresh via {usergate} ...")

    # --- Try every combination of path × method ---
    for path in REFRESH_PATHS:
        # 1. Cookie variants
        for cookie_name in COOKIE_NAMES_TO_TRY:
            result = _try_cookie(usergate, path, cookie_name, refresh_token)
            if result:
                access_token = _extract_access_token(result)
                if access_token:
                    print(f"[refresh] SUCCESS via cookie {cookie_name!r} on {path}")
                    creds["access_token"] = access_token
                    if "refresh_token" in result or "refreshToken" in result:
                        creds["refresh_token"] = result.get("refresh_token") or result.get("refreshToken")
                    new_expiry = result.get("expires_in")
                    if new_expiry:
                        creds["session_expiration"] = int(time.time()) + int(new_expiry)
                    save_credentials(creds)
                    return True

        # 2. Bearer auth
        result = _try_bearer(usergate, path, refresh_token)
        if result:
            access_token = _extract_access_token(result)
            if access_token:
                print(f"[refresh] SUCCESS via Bearer on {path}")
                creds["access_token"] = access_token
                save_credentials(creds)
                return True

        # 3. JSON body
        result = _try_body(usergate, path, refresh_token)
        if result:
            access_token = _extract_access_token(result)
            if access_token:
                print(f"[refresh] SUCCESS via JSON body on {path}")
                creds["access_token"] = access_token
                save_credentials(creds)
                return True

    print("[refresh] All refresh attempts failed. You'll need to run: bullpen login")
    print("[refresh] Then approve at: https://app.bullpen.fi/device")
    return False


if __name__ == "__main__":
    force = "--force" in sys.argv
    success = refresh_token_if_needed(force=force)
    sys.exit(0 if success else 1)
