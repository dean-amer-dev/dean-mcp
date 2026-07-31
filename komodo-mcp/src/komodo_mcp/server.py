"""
Komodo MCP Server — FastMCP tools for managing Komodo stacks,
clusters, and actions via the Komodo HTTP API.

This module provides a collection of MCP tools that wrap the
Komodo REST API for stack lifecycle management (deploy, sync,
delete, redeploy), health checks, and action queries.
"""

import httpx
import os

from fastmcp import FastMCP

# ──────────────────────── Configuration ────────────────────────


KOMODO_BASE = os.environ.get("KOMODO_BASE", "http://localhost:9120")
KOMODO_API_KEY = os.environ.get("KOMODO_API_KEY", "")
KOMODO_API_SECRET = os.environ.get("KOMODO_API_SECRET", "")

mcp = FastMCP(name="komodo-mcp")


# ──────────────────────── API helpers ────────────────────────


_READ_PATH = "/read/{action}"
_EXECUTE_PATH = "/execute/{action}"
_WRITE_PATH = "/write/{action}"

_METHODS_FOR = {
    "ListStacks": _READ_PATH,
    "ListActions": _READ_PATH,
    "GetStack": _READ_PATH,
    "GetStackActionState": _READ_PATH,
    "GetAction": _READ_PATH,
    "RunSync": _WRITE_PATH,
    "DeployStack": _WRITE_PATH,
    "DeleteStack": _WRITE_PATH,
    "RedeployStack": _WRITE_PATH,
    "UndeployStack": _WRITE_PATH,
}


def _headers() -> dict:
    """Return HTTP headers including auth if configured."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if KOMODO_API_KEY:
        headers["X-Api-Key"] = KOMODO_API_KEY
    if KOMODO_API_SECRET:
        headers["X-Api-Secret"] = KOMODO_API_SECRET
    return headers


def _resolve_path(action: str) -> str:
    """Return the correct path template for a given action."""
    return _METHODS_FOR.get(action, _READ_PATH)


def _jsonrpc_payload(action: str, **params) -> dict:
    """Build a JSON-RPC-style request body with id, method, and params."""
    return {
        "id": 1,
        "method": action,
        "params": params or {},
    }


def _make_request(action: str, **params) -> dict | list:
    """Helper: POST the bare params dict as the JSON body.

    The real Komodo API expects the POST body to be the bare params
    dict directly (e.g. json.dumps(body or {})), NOT wrapped in a
    JSON-RPC-style {"id":1,"method":...,"params":...} envelope.
    """
    path = _resolve_path(action).format(action=action)
    url = f"{KOMODO_BASE}{path}"
    try:
        resp = httpx.post(
            url,
            json=params or {},
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Normalize: some responses wrap result in a "result" key
        if "result" in data:
            return data["result"]
        if "data" in data:
            return data["data"]
        return data
    except httpx.HTTPStatusError as exc:
        return {"error": f"{exc.response.status_code}: {exc.response.text[:300]}"}
    except Exception as exc:
        return {"error": str(exc)}


def _normalize_result(result):
    """Normalize a response to handle both bare lists and dicts.

    The real Komodo API returns bare JSON arrays for list-type reads
    (e.g. ListStacks -> [{...}, {...}]) but objects for single-item reads
    (e.g. GetStack -> {"name": "...", ...}). This helper ensures the
    caller can safely use .get() regardless of the shape.
    """
    return result


def _extract_list(result):
    """Extract a list from the result, handling both bare lists and wrapped dicts.

    For bare lists: returns the list directly.
    For dicts: looks for a list value in common keys like 'stacks', 'actions',
    'result', 'data', or 'items'. Falls back to treating the dict as a
    single-item result (returns [result] for single-item reads that need list iteration).
    """
    if isinstance(result, list):
        return result
    # If it's a dict, check for a list-valued key
    for key in ("stacks", "actions", "items", "result", "data"):
        val = result.get(key)
        if isinstance(val, list):
            return val
    return [result] if result else []


# ──────────────────────── Tools ────────────────────────


@mcp.tool()
def list_stacks(namespace: str = "") -> str:
    """
    List all Komodo stacks.

    Optionally filters by namespace.

    Returns a formatted list of stack names and their status.
    """
    params: dict[str, str] = {}
    if namespace:
        params["namespace"] = namespace
    result = _make_request("ListStacks", **params)
    if "error" in result:
        return f"Error listing stacks: {result['error']}"
    # Handle both bare list [{...}, ...] and wrapped {"stacks": [{...}, ...]}
    stacks = _extract_list(result)
    return f"Found {len(stacks)} stacks:\n" + "\n".join(
        f"  - {s.get('name', '?')} ({s.get('namespace', '?')}): "
        f"status={s.get('status', '?')}"
        for s in stacks
    )


@mcp.tool()
def get_stack(name: str = "", namespace: str = "") -> str:
    """
    Get detailed information about a specific Komodo stack.

    Args:
        name: The name of the stack.
        namespace: The namespace the stack is in (optional).

    Returns detailed status, version, health, and configuration for the stack.
    """
    params: dict[str, str] = {}
    if name:
        params["name"] = name
    if namespace:
        params["namespace"] = namespace
    result = _make_request("GetStack", **params)
    if "error" in result:
        return f"Error getting stack: {result['error']}"
    return f"Stack '{result.get('name', '')}':\n" + "\n".join(
        f"  {k}: {v}" for k, v in result.items() if k != "stacks"
    )


@mcp.tool()
def get_stack_action_state(
    stack_name: str = "",
    action_name: str = "",
) -> str:
    """
    Check the current state of an action on a Komodo stack.

    Useful for verifying if a deploy, sync, or delete is in progress
    or has completed.

    Args:
        stack_name: The stack to check.
        action_name: The specific action to check (e.g. 'DeployStack').

    Returns the action state and status.
    """
    params: dict[str, str] = {}
    if stack_name:
        params["stackName"] = stack_name
    if action_name:
        params["actionName"] = action_name
    result = _make_request("GetStackActionState", **params)
    if "error" in result:
        return f"Error getting action state: {result['error']}"
    return f"Action '{result.get('actionName', '')}' on stack "
    f"'{result.get('stackName', '')}': status={result.get('state', 'unknown')}"


@mcp.tool()
def run_sync(
    stack_name: str = "",
    namespace: str = "",
    timeout: int = 300,
) -> str:
    """
    Trigger a resource sync for one or all Komodo stacks.

    Forces the stack to reconcile its desired state with the actual
    state in the cluster.

    Args:
        stack_name: Specific stack to sync (empty for all stacks).
        namespace: Optional namespace filter.
        timeout: Maximum time to wait for sync completion (seconds).

    Returns sync job details and status.
    """
    params: dict[str, str | int] = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    params["timeout"] = timeout
    result = _make_request("RunSync", **params)
    if "error" in result:
        return f"Error running sync: {result['error']}"
    return (
        f"Sync triggered for stack '{result.get('stackName', 'all')}'.\n"
        f"Sync ID: {result.get('syncId', 'N/A')}, "
        f"status: {result.get('status', 'running')}"
    )


@mcp.tool()
def deploy_stack(
    stack_name: str = "",
    version: str = "latest",
    namespace: str = "",
    wait: bool = True,
) -> str:
    """
    Deploy a Komodo stack to the target Kubernetes cluster.

    Creates or updates the stack with the specified version.
    Optionally waits for the deployment to complete.

    Args:
        stack_name: Name of the stack to deploy (required).
        version: Target version to deploy (default: 'latest').
        namespace: Target namespace (default: '').
        wait: Whether to wait for completion (default: True).

    Returns deployment result with status and details.
    """
    params: dict[str, str | bool | int] = {}
    if stack_name:
        params["stackName"] = stack_name
    params["version"] = version
    if namespace:
        params["namespace"] = namespace
    params["wait"] = wait
    result = _make_request("DeployStack", **params)
    if "error" in result:
        return f"Error deploying stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' deployed.\n"
        f"Status: {result.get('status', 'deploying')}, "
        f"version: {result.get('version', 'unknown')}"
    )


@mcp.tool()
def delete_stack(
    stack_name: str = "",
    namespace: str = "",
    force: bool = False,
) -> str:
    """
    Delete a Komodo stack from the target cluster.

    Args:
        stack_name: Name of the stack to delete (required).
        namespace: Target namespace (default: '').
        force: Whether to force deletion (default: False).

    Returns deletion result with status and details.
    """
    params: dict[str, str | bool] = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    params["force"] = force
    result = _make_request("DeleteStack", **params)
    if "error" in result:
        return f"Error deleting stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' deleted.\n"
        f"Status: {result.get('status', 'deleted')}"
    )


@mcp.tool()
def redeploy_stack(
    stack_name: str = "",
    version: str = "",
    namespace: str = "",
) -> str:
    """
    Redeploy a Komodo stack.

    Args:
        stack_name: Name of the stack to redeploy (required).
        version: Optional target version (empty to keep current).
        namespace: Target namespace (default: '').

    Returns redeployment result with status and details.
    """
    params: dict[str, str | bool] = {}
    if stack_name:
        params["stackName"] = stack_name
    if version:
        params["version"] = version
    if namespace:
        params["namespace"] = namespace
    result = _make_request("RedeployStack", **params)
    if "error" in result:
        return f"Error redeploying stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' redeployed.\n"
        f"Status: {result.get('status', 'redeploying')}"
    )


@mcp.tool()
def undeploy_stack(
    stack_name: str = "",
    namespace: str = "",
) -> str:
    """
    Undeploy a Komodo stack from the target cluster.

    Args:
        stack_name: Name of the stack to undeploy (required).
        namespace: Target namespace (default: '').

    Returns undeployment result with status and details.
    """
    params: dict[str, str] = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    result = _make_request("UndeployStack", **params)
    if "error" in result:
        return f"Error undeploying stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' undeployed.\n"
        f"Status: {result.get('status', 'undeployed')}"
    )


@mcp.tool()
def list_actions() -> str:
    """
    List all available Komodo actions and their descriptions.

    Returns a list of available actions with their types
    (read, execute, write) and descriptions.
    """
    result = _make_request("ListActions")
    if "error" in result:
        return f"Error listing actions: {result['error']}"
    # Handle both bare list [{...}, ...] and wrapped {"actions": [{...}, ...]}
    actions = _extract_list(result)
    if not actions:
        return "No actions available."
    return "Available actions:\n" + "\n".join(
        f"  - {a.get('name', '?')} ({a.get('type', '?')}): "
        f"{a.get('description', '')}"
        for a in actions
    )


@mcp.tool()
def get_action(
    action_name: str = "",
    action_id: str = "",
) -> str:
    """
    Get details about a specific Komodo action.

    Args:
        action_name: Name of the action.
        action_id: ID of the action (alternative to name).

    Returns action details including status, parameters, and result.
    """
    params: dict[str, str] = {}
    if action_name:
        params["actionName"] = action_name
    if action_id:
        params["actionId"] = action_id
    result = _make_request("GetAction", **params)
    if "error" in result:
        return f"Error getting action: {result['error']}"
    return (
        f"Action '{result.get('name', '')}':\n"
        + "\n".join(
            f"  {k}: {v}" for k, v in result.items()
            if k not in ("actions", "stacks")
        )
    )


# ──────────────────────── Health ────────────────────────


@mcp.tool()
def health() -> str:
    """
    Check the health of this Komodo MCP server and its connection to
    the Komodo API.

    Returns the status, Komodo API URL, and whether auth is configured.
    """
    healthy = True
    details = {
        "server": "komodo-mcp",
        "komodo_api_url": KOMODO_BASE,
        "api_key_configured": bool(KOMODO_API_KEY),
        "api_secret_configured": bool(KOMODO_API_SECRET),
    }

    # Quick check to Komodo API
    try:
        resp = httpx.get(
            f"{KOMODO_BASE}/read/ListStacks",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            details["komodo_api"] = "healthy"
        else:
            details["komodo_api"] = f"unhealthy (HTTP {resp.status_code})"
            healthy = False
    except Exception as exc:
        details["komodo_api"] = f"error ({exc})"
        healthy = False

    return f"Health: {'OK' if healthy else 'DEGRADED'}\n" + "\n".join(
        f"  {k}: {v}" for k, v in details.items()
    )


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
