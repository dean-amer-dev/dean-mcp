"""bws-mcp: Bitwarden Secrets Manager MCP server."""

from __future__ import annotations

import json
import os
import subprocess

from fastmcp import FastMCP

_BWS_TOKEN = os.environ.get("BWS_ACCESS_TOKEN", "")

mcp = FastMCP(
    "bws-mcp",
    instructions="Bitwarden Secrets Manager proxy. Get, list, create, update, and delete secrets.",
)


def _bws(*args: str) -> tuple[int, str, str]:
    if not _BWS_TOKEN:
        return 1, "", "BWS_ACCESS_TOKEN not configured."
    env = {**os.environ, "BWS_ACCESS_TOKEN": _BWS_TOKEN}
    r = subprocess.run(
        ["bws", *args, "--output", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode, r.stdout, r.stderr


def _all_secrets() -> list[dict] | None:
    rc, stdout, _ = _bws("secret", "list")
    if rc != 0:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


@mcp.tool()
def get_secret(key: str) -> dict:
    """Retrieve a secret value by its BWS key name."""
    if not _BWS_TOKEN:
        return {"error": "BWS_ACCESS_TOKEN not configured on this server."}
    secrets = _all_secrets()
    if secrets is None:
        return {"error": "Failed to list secrets from BWS."}
    for s in secrets:
        if s.get("key") == key:
            return {"key": key, "value": s["value"], "id": s.get("id")}
    return {"error": f"Secret '{key}' not found."}


@mcp.tool()
def list_secret_names() -> dict:
    """List all BWS secret key names (no values)."""
    secrets = _all_secrets()
    if secrets is None:
        return {"error": "Failed to list secrets from BWS."}
    return {"keys": [s["key"] for s in secrets], "count": len(secrets)}


@mcp.tool()
def create_secret(key: str, value: str, note: str = "") -> dict:
    """Create a new secret in BWS."""
    cmd = ["secret", "create", key, value]
    if note:
        cmd += ["--note", note]
    rc, stdout, stderr = _bws(*cmd)
    if rc != 0:
        return {"error": f"bws create failed: {stderr[:500]}"}
    try:
        return {"status": "created", "secret": json.loads(stdout)}
    except json.JSONDecodeError:
        return {"status": "created"}


@mcp.tool()
def update_secret(key: str, value: str) -> dict:
    """Update an existing BWS secret's value by key name."""
    existing = get_secret(key)
    if "error" in existing:
        return existing
    secret_id = existing.get("id")
    if not secret_id:
        return {"error": f"No ID found for secret '{key}'"}
    rc, stdout, stderr = _bws("secret", "edit", secret_id, "--value", value)
    if rc != 0:
        return {"error": f"bws edit failed: {stderr[:500]}"}
    return {"status": "updated", "key": key}


@mcp.tool()
def delete_secret(key: str) -> dict:
    """Delete a BWS secret by key name."""
    existing = get_secret(key)
    if "error" in existing:
        return existing
    secret_id = existing.get("id")
    if not secret_id:
        return {"error": f"No ID found for secret '{key}'"}
    rc, stdout, stderr = _bws("secret", "delete", secret_id)
    if rc != 0:
        return {"error": f"bws delete failed: {stderr[:500]}"}
    return {"status": "deleted", "key": key}


if __name__ == "__main__":
    mcp.run()
