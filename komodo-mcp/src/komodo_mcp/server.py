"""Komodo MCP server — stack status, sync, deploy, and delete."""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP

mcp = FastMCP("komodo-mcp")

KOMODO_BASE = os.environ.get("KOMODO_API_URL", "https://komodo.amer.dev")
KOMODO_API_KEY = os.environ.get("KOMODO_API_KEY", "")
KOMODO_API_SECRET = os.environ.get("KOMODO_API_SECRET", "")

# HTTP method categories for the Komodo JSON-RPC-style REST API
_READ_PATH = "/read/{action}"
_EXECUTE_PATH = "/execute/{action}"
_WRITE_PATH = "/write/{action}"

_METHODS_FOR = {
    "ListStacks": _READ_PATH,
    "GetStack": _READ_PATH,
    "GetStackActionState": _READ_PATH,
    "ListActions": _READ_PATH,
    "GetAction": _READ_PATH,
    "GetActionState": _READ_PATH,
    "RunSync": _EXECUTE_PATH,
    "DeployStack": _EXECUTE_PATH,
    "UndeployStack": _EXECUTE_PATH,
    "RedeployStack": _EXECUTE_PATH,
    "DeleteStack": _WRITE_PATH,
    "DeleteStackByName": _WRITE_PATH,
    "ScaleStack": _WRITE_PATH,
    "UpdateStack": _WRITE_PATH,
}


def _headers() -> dict[str, str]:
    """Return the auth headers expected by Komodo."""
    h: dict[str, str] = {}
    if KOMODO_API_KEY:
        h["X-Api-Key"] = KOMODO_API_KEY
    if KOMODO_API_SECRET:
        h["X-Api-Secret"] = KOMODO_API_SECRET
    return h


def _resolve_path(action: str) -> str:
    """Return the correct path template for a given action."""
    return _METHODS_FOR.get(action, _READ_PATH)


def _jsonrpc_payload(**params) -> dict:
    """Build a JSON-RPC-style request body with id, method, and params."""
    return {
        "id": 1,
        "method": action,
        "params": params or {},
    }


def _make_request(action: str, **params) -> dict:
    """Helper: POST to the appropriate /{read,execute,write}/{Action}."""
    path = _resolve_path(action).format(action=action)
    url = f"{KOMODO_BASE}{path}"
    payload = _jsonrpc_payload(**params)
    try:
        resp = httpx.post(
            url,
            json=payload,
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


# ──────────────────────── Tools ────────────────────────


@mcp.tool()
def list_stacks() -> str:
    """
    List all Komodo stacks and their current status.

    Returns a JSON string with stack details including name, namespace,
    deployment status, version, and health.
    """
    result = _make_request("ListStacks")
    if "error" in result:
        return f"Error listing stacks: {result['error']}"
    return f"Found {len(result.get('stacks', []))} stacks:\n" + "\n".join(
        f"  - {s.get('name', '?')} ({s.get('namespace', '?')}): "
        f"status={s.get('status', '?')}"
        for s in result.get("stacks", [])
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
    version: str = "",
    namespace: str = "",
    wait: bool = True,
) -> str:
    """
    Deploy a Komodo stack.

    Creates or updates the stack with the current desired state.

    Args:
        stack_name: Name of the stack to deploy.
        version: Target version to deploy (optional).
        namespace: Namespace for the stack.
        wait: Whether to wait for deployment to complete.

    Returns deployment status and details.
    """
    params: dict[str, str | bool | int] = {}
    if stack_name:
        params["stackName"] = stack_name
    if version:
        params["version"] = version
    if namespace:
        params["namespace"] = namespace
    params["wait"] = wait
    result = _make_request("DeployStack", **params)
    if "error" in result:
        return f"Error deploying stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' deployed successfully.\n"
        f"Version: {result.get('version', 'latest')}, "
        f"status: {result.get('status', 'deployed')}"
    )


@mcp.tool()
def delete_stack(
    stack_name: str = "",
    namespace: str = "",
    force: bool = False,
) -> str:
    """
    Delete a Komodo stack.

    Removes the stack resources from the cluster.

    Args:
        stack_name: Name of the stack to delete.
        namespace: Namespace of the stack.
        force: If True, force deletion even if stack is not healthy.

    Returns deletion confirmation and details.
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
        f"Stack '{result.get('stackName', stack_name or 'unknown')}' deleted.\n"
        f"Namespace: {result.get('namespace', 'default')}, "
        f"forced: {result.get('force', False)}"
    )


@mcp.tool()
def redeploy_stack(
    stack_name: str = "",
    namespace: str = "",
    version: str = "",
) -> str:
    """
    Redeploy a Komodo stack (restart with current config).

    Useful for applying configuration changes or recovering from
    a failed deployment.

    Args:
        stack_name: Name of the stack to redeploy.
        namespace: Namespace of the stack.
        version: Optional target version.

    Returns redeployment status.
    """
    params: dict[str, str | bool] = {}
    if stack_name:
        params["stackName"] = stack_name
    if namespace:
        params["namespace"] = namespace
    if version:
        params["version"] = version
    result = _make_request("RedeployStack", **params)
    if "error" in result:
        return f"Error redeploying stack: {result['error']}"
    return (
        f"Stack '{result.get('stackName', '')}' redeployed.\n"
        f"Status: {result.get('status', 'redeployed')}"
    )


@mcp.tool()
def undeploy_stack(
    stack_name: str = "",
    namespace: str = "",
) -> str:
    """
    Undeploy (remove resources of) a Komodo stack without deleting it.

    Args:
        stack_name: Name of the stack to undeploy.
        namespace: Namespace of the stack.

    Returns undeployment status.
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
    actions = result.get("actions", [])
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
