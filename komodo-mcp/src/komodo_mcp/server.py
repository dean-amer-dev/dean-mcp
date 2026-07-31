"""Komodo MCP server — view stack status, trigger syncs, deploy and delete stacks."""

import os
from typing import NotRequired, TypedDict

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KOMODO_BASE_URL = os.getenv("KOMODO_API_URL", "https://komodo.amer.dev")
KOMODO_API_KEY = os.getenv("KOMODO_API_KEY", "")
KOMODO_API_SECRET = os.getenv("KOMODO_API_SECRET", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    """Return common request headers with authentication."""
    return {
        "Content-Type": "application/json",
        "X-Api-Key": KOMODO_API_KEY,
        "X-Api-Secret": KOMODO_API_SECRET,
    }


def _get(action: str, params: dict | None = None) -> dict:
    """POST to /read/{action} for read operations."""
    url = f"{KOMODO_BASE_URL}/read/{action}"
    try:
        resp = httpx.post(url, headers=_headers(), json=params or {}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", data)
    except Exception as e:
        return {"error": f"GET {action}: {e}"}


def _execute(action: str, params: dict | None = None) -> dict:
    """POST to /execute/{action} for action operations (e.g. RunSync)."""
    url = f"{KOMODO_BASE_URL}/execute/{action}"
    try:
        resp = httpx.post(url, headers=_headers(), json=params or {}, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", data)
    except Exception as e:
        return {"error": f"EXECUTE {action}: {e}"}


def _write(action: str, params: dict | None = None) -> dict:
    """POST to /write/{action} for mutation operations (e.g. DeleteStack)."""
    url = f"{KOMODO_BASE_URL}/write/{action}"
    try:
        resp = httpx.post(url, headers=_headers(), json=params or {}, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", data)
    except Exception as e:
        return {"error": f"WRITE {action}: {e}"}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

mcp = FastMCP("komodo-mcp")


@mcp.tool()
def list_stacks() -> dict:
    """List all Komodo stacks and their current status.

    Returns a list of stacks with their names, namespaces, and status
    information from the Komodo API.
    """
    return _get("ListStacks")


@mcp.tool()
def get_stack(stack_name: str = "", namespace: str = "") -> dict:
    """Get detailed information about a specific Komodo stack.

    Args:
        stack_name: Name of the stack. If empty, returns all stacks.
        namespace: Optional namespace to filter by.
    """
    params: dict = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    return _get("GetStack", params)


@mcp.tool()
def get_stack_action_state(stack_name: str, action: str) -> dict:
    """Get the current state of a specific action for a stack.

    Args:
        stack_name: Name of the stack.
        action: The action name to query (e.g. "Deploy", "Sync", "Delete").
    """
    params = {
        "stackName": stack_name,
        "action": action,
    }
    return _get("GetStackActionState", params)


@mcp.tool()
def run_sync(stack_name: str = "", namespace: str = "") -> dict:
    """Trigger a resource sync for a stack.

    Args:
        stack_name: Name of the stack to sync. If empty, syncs all stacks.
        namespace: Optional namespace to filter by.
    """
    params: dict = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    return _execute("RunSync", params)


@mcp.tool()
def deploy_stack(stack_name: str, namespace: str = "") -> dict:
    """Deploy a stack in Komodo.

    Args:
        stack_name: Name of the stack to deploy.
        namespace: Optional namespace for the stack.
    """
    params = {"stackName": stack_name}
    if namespace:
        params["namespace"] = namespace
    return _execute("DeployStack", params)


@mcp.tool()
def delete_stack(stack_name: str, namespace: str = "") -> dict:
    """Delete a stack in Komodo.

    Args:
        stack_name: Name of the stack to delete.
        namespace: Optional namespace for the stack.
    """
    params = {"stackName": stack_name}
    if namespace:
        params["namespace"] = namespace
    return _write("DeleteStack", params)


@mcp.tool()
def get_health() -> dict:
    """Check the health of the Komodo API connection.

    Verifies that the API key and secret are configured and that the
    Komodo API at https://komodo.amer.dev is reachable.
    """
    if not KOMODO_API_KEY:
        return {"status": "unhealthy", "error": "KOMODO_API_KEY not set"}
    if not KOMODO_API_SECRET:
        return {"status": "unhealthy", "error": "KOMODO_API_SECRET not set"}

    try:
        resp = httpx.post(
            f"{KOMODO_BASE_URL}/read/ListStacks",
            headers=_headers(),
            json={},
            timeout=10,
        )
        resp.raise_for_status()
        return {"status": "healthy", "komodo_url": KOMODO_BASE_URL}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


# ---------------------------------------------------------------------------
# Health endpoint (for Kubernetes / Docker health checks)
# ---------------------------------------------------------------------------

@mcp.http("GET", "/health")
def health_endpoint(request) -> dict:
    """Simple health check endpoint.

    Returns JSON with the health status of the MCP server and its
    connection to the Komodo API.
    """
    return get_health()


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
