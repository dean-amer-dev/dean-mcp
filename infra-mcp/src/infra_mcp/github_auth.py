"""GitHub App JWT and installation token helpers."""

from __future__ import annotations

import time

import httpx
import jwt as pyjwt


def _make_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    payload = {"iss": app_id, "iat": now - 60, "exp": now + 540}
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_token(app_id: str, installation_id: str, private_key_pem: str) -> str:
    """Exchange GitHub App credentials for a short-lived installation token (valid 1 hour)."""
    jwt_token = _make_jwt(app_id, private_key_pem)
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]
