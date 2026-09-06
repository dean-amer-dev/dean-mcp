"""
Komodo MCP Server — FastMCP tools for managing Komodo stacks,
clusters, and actions via the Komodo HTTP API.

This module provides a collection of MCP tools that wrap the
Komodo REST API for stack lifecycle management (deploy, sync,
delete, redeploy), health checks, and action queries.
"""

import time

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

# Komodo's HTTP API has three routers — /read, /write, /execute —
# each backed by a distinct tagged enum (ReadRequest, WriteRequest,
# ExecuteRequest). An action name only exists in exactly one of them;
# routing it to the wrong router 500s with "unknown variant".
_METHODS_FOR = {
    "ListStacks": _READ_PATH,
    "ListActions": _READ_PATH,
    "ListResourceSyncs": _READ_PATH,
    "GetStack": _READ_PATH,
    "GetStackActionState": _READ_PATH,
    "GetAction": _READ_PATH,
    "GetStackLog": _READ_PATH,
    "SearchStackLog": _READ_PATH,
    "ListUpdates": _READ_PATH,
    "GetUpdate": _READ_PATH,
    "RunSync": _EXECUTE_PATH,
    "DeployStack": _EXECUTE_PATH,
    "DestroyStack": _EXECUTE_PATH,
    "DeleteStack": _WRITE_PATH,
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
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data
    except httpx.HTTPStatusError as exc:
        return {"error": f"{exc.response.status_code}: {exc.response.text[:300]}"}
    except Exception as exc:
        return {"error": str(exc)}


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
    for key in ("stacks", "actions", "syncs", "items", "result", "data"):
        val = result.get(key)
        if isinstance(val, list):
            return val
    return [result] if result else []


def _format_log(log) -> str:
    """Mirror Komodo's own Log.combined(): stdout+stderr if both present,
    whichever one is present if only one, "No log" if neither."""
    if not isinstance(log, dict):
        return str(log)
    stdout = log.get("stdout", "")
    stderr = log.get("stderr", "")
    if stdout and stderr:
        return f"stdout: {stdout}\n\nstderr: {stderr}"
    if stdout:
        return stdout
    if stderr:
        return stderr
    return "No log"


def _wait_for_stack_action_state(
    stack_name: str, flag: str, timeout: int = 60, interval: int = 2
) -> dict | None:
    """Poll GetStackActionState until `flag` clears or timeout elapses.

    Client-side convenience only — Komodo's DeployStack/DestroyStack
    requests have no server-side "wait" field.
    """
    elapsed = 0
    while elapsed < timeout:
        result = _make_request("GetStackActionState", stack=stack_name)
        if isinstance(result, dict) and "error" not in result and not result.get(flag, False):
            return result
        time.sleep(interval)
        elapsed += interval
    return None


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
    if isinstance(result, dict) and "error" in result:
        return f"Error listing stacks: {result['error']}"
    stacks = _extract_list(result)
    return f"Found {len(stacks)} stacks:\n" + "\n".join(
        f"  - {s.get('name', '?')} ({s.get('namespace', '?')}): "
        f"status={s.get('status', s.get('info', {}).get('state', '?'))}"
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
    if isinstance(result, dict) and "error" in result:
        return f"Error getting stack: {result['error']}"
    return f"Stack '{result.get('name', '')}':\n" + "\n".join(
        f"  {k}: {v}" for k, v in result.items() if k != "stacks"
    )


@mcp.tool()
def get_stack_action_state(stack_name: str = "") -> str:
    """
    Check which actions are currently in progress for a Komodo stack
    (pulling, deploying, starting, restarting, pausing, unpausing,
    stopping, destroying).

    Useful for verifying if a deploy, sync, or destroy triggered via
    deploy_stack/run_sync/undeploy_stack is still running or has finished.

    Args:
        stack_name: The stack to check (required).

    Returns which action flags are currently true for the stack.
    """
    result = _make_request("GetStackActionState", stack=stack_name)
    if isinstance(result, dict) and "error" in result:
        return f"Error getting action state: {result['error']}"
    if not isinstance(result, dict):
        return f"Unexpected response: {result}"
    active = [k for k, v in result.items() if v]
    return f"Stack '{stack_name}' action state:\n" + (
        "  " + ", ".join(active) if active else "  no actions in progress"
    )


@mcp.tool()
def list_resource_syncs() -> str:
    """
    List all Komodo ResourceSync resources.

    A ResourceSync is a distinct resource type from a Stack — it
    represents a git-backed sync configuration (e.g. a gitops repo).
    Use this to find the correct `sync_name` to pass to run_sync;
    it will not generally match a stack name.

    Returns a formatted list of resource sync names and ids.
    """
    result = _make_request("ListResourceSyncs")
    if isinstance(result, dict) and "error" in result:
        return f"Error listing resource syncs: {result['error']}"
    syncs = _extract_list(result)
    return f"Found {len(syncs)} resource syncs:\n" + "\n".join(
        f"  - {s.get('name', '?')} (id={s.get('id', '?')})" for s in syncs
    )


@mcp.tool()
def get_stack_log(
    stack_name: str = "",
    services: list[str] | None = None,
    tail: int = 100,
    timestamps: bool = False,
) -> str:
    """
    Get a Komodo stack's container logs (`docker compose logs` equivalent).

    Hits the underlying Periphery server directly for the freshest data
    (not cached in Core).

    Args:
        stack_name: Name or id of the stack (required).
        services: Filter to specific services (empty includes all services).
        tail: Number of lines of log tail to include (server max: 5000).
        timestamps: Enable `--timestamps`.

    Returns the combined stdout/stderr log output.
    """
    params: dict = {
        "stack": stack_name,
        "services": services or [],
        "tail": tail,
        "timestamps": timestamps,
    }
    result = _make_request("GetStackLog", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error getting stack log: {result['error']}"
    return f"Log for stack '{stack_name}':\n{_format_log(result)}"


@mcp.tool()
def search_stack_log(
    stack_name: str = "",
    terms: list[str] | None = None,
    services: list[str] | None = None,
    combinator: str = "OR",
    invert: bool = False,
    timestamps: bool = False,
) -> str:
    """
    Search a Komodo stack's container log tail using `grep`.

    Hits the underlying Periphery server directly for the freshest data
    (not cached in Core).

    Args:
        stack_name: Name or id of the stack (required).
        terms: The terms to search for (required).
        services: Filter to specific services (empty includes all services).
        combinator: "AND" requires all terms on a line, "OR" (default)
            requires any one term.
        invert: Return lines that DON'T match the terms/combinator instead.
        timestamps: Enable `--timestamps`.

    Returns the matching log lines.
    """
    params: dict = {
        "stack": stack_name,
        "services": services or [],
        "terms": terms or [],
        "combinator": combinator,
        "invert": invert,
        "timestamps": timestamps,
    }
    result = _make_request("SearchStackLog", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error searching stack log: {result['error']}"
    return f"Search results for stack '{stack_name}':\n{_format_log(result)}"


@mcp.tool()
def list_updates(
    target_type: str = "",
    target_id: str = "",
    page: int = 0,
) -> str:
    """
    List Komodo Updates — the execution history of every action Komodo
    has performed (deploys, syncs, destroys, etc.), most recent first.

    Args:
        target_type: Resource type to filter to, e.g. "Stack". Must be
            given together with target_id, otherwise ignored.
        target_id: Mongo `_id` of the resource to filter to (get this
            from get_stack's `_id.$oid`). Must be given together with
            target_type.
        page: Page of results, 0 = most recent (default).

    Returns a formatted list of updates: id, operation, status, success,
    start_ts, target. Use get_update on an id for the full record.
    """
    params: dict = {"page": page}
    if target_type and target_id:
        params["query"] = {"target.type": target_type, "target.id": target_id}
    result = _make_request("ListUpdates", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error listing updates: {result['error']}"
    updates = result.get("updates", []) if isinstance(result, dict) else []
    next_page = result.get("next_page") if isinstance(result, dict) else None
    header = f"Found {len(updates)} updates (next_page={next_page}):"
    if not updates:
        return header
    return header + "\n" + "\n".join(
        f"  - id={u.get('id', '?')} op={u.get('operation', '?')} "
        f"status={u.get('status', '?')} success={u.get('success', '?')} "
        f"start_ts={u.get('start_ts', '?')} target={u.get('target', '?')}"
        for u in updates
    )


@mcp.tool()
def get_update(update_id: str = "") -> str:
    """
    Get the full record for a single Komodo Update, including every
    command Komodo ran and its real stdout/stderr — this is the only way
    to see what a pre_deploy script actually did (or didn't do).

    Args:
        update_id: The update's id (required, from list_updates).

    Returns status, success, start_ts/end_ts, and every log entry
    (stage, command, success, combined stdout/stderr).
    """
    result = _make_request("GetUpdate", id=update_id)
    if isinstance(result, dict) and "error" in result:
        return f"Error getting update: {result['error']}"
    if not isinstance(result, dict):
        return f"Unexpected response: {result}"
    logs = result.get("logs", [])
    header = (
        f"Update '{update_id}':\n"
        f"  status: {result.get('status', '?')}\n"
        f"  success: {result.get('success', '?')}\n"
        f"  start_ts: {result.get('start_ts', '?')}\n"
        f"  end_ts: {result.get('end_ts', '?')}\n"
    )
    if not logs:
        return header + "  (no logs)"
    return header + "\n".join(
        f"  --- stage: {log.get('stage', '?')} ---\n"
        f"  command: {log.get('command', '?')}\n"
        f"  success: {log.get('success', '?')}\n"
        f"  {_format_log(log)}"
        for log in logs
    )


@mcp.tool()
def run_sync(
    sync_name: str = "",
    resource_type: str = "",
    resources: list[str] | None = None,
) -> str:
    """
    Trigger a Komodo ResourceSync to run, reconciling the desired
    (git) state with the actual state.

    Args:
        sync_name: Name or id of the ResourceSync to run (required).
            This is a ResourceSync, not a Stack — use list_resource_syncs
            to find the correct name.
        resource_type: Optionally restrict the sync to one resource type
            (combine with `resources`).
        resources: Optionally restrict the sync to specific resource
            names/ids of `resource_type`.

    Returns the resulting Update status.
    """
    params: dict = {"sync": sync_name}
    if resource_type:
        params["resource_type"] = resource_type
    if resources:
        params["resources"] = resources
    result = _make_request("RunSync", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error running sync: {result['error']}"
    status = result.get("status", result) if isinstance(result, dict) else result
    return f"Sync '{sync_name}' triggered.\nUpdate status: {status}"


@mcp.tool()
def deploy_stack(
    stack_name: str = "",
    services: list[str] | None = None,
    stop_time: int | None = None,
    wait: bool = True,
) -> str:
    """
    Deploy a Komodo stack (`docker compose up`).

    Creates or updates the running containers to match the stack's
    latest configuration.

    Args:
        stack_name: Name or id of the stack to deploy (required).
        services: Specific services to deploy (empty deploys all services).
        stop_time: Override the default termination max time, only used
            if the stack needs to be taken down first.
        wait: Poll get_stack_action_state until the deploy finishes,
            up to 60s (default: True). This is client-side polling —
            Komodo's API has no server-side wait.

    Returns the resulting Update status.
    """
    params: dict = {"stack": stack_name, "services": services or []}
    if stop_time is not None:
        params["stop_time"] = stop_time
    result = _make_request("DeployStack", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error deploying stack: {result['error']}"
    if wait:
        _wait_for_stack_action_state(stack_name, "deploying")
    status = result.get("status", result) if isinstance(result, dict) else result
    return f"Stack '{stack_name}' deploy triggered.\nUpdate status: {status}"


@mcp.tool()
def redeploy_stack(
    stack_name: str = "",
    services: list[str] | None = None,
) -> str:
    """
    Redeploy a Komodo stack.

    Komodo has no separate "redeploy" action — this is a thin wrapper
    around deploy_stack (deploying again is the redeploy).

    Args:
        stack_name: Name or id of the stack to redeploy (required).
        services: Specific services to redeploy (empty redeploys all services).

    Returns the resulting Update status.
    """
    return deploy_stack(stack_name=stack_name, services=services)


@mcp.tool()
def undeploy_stack(
    stack_name: str = "",
    services: list[str] | None = None,
    remove_orphans: bool = False,
    stop_time: int | None = None,
) -> str:
    """
    Tear down a Komodo stack's running containers (`docker compose down`).

    This does NOT delete the Stack resource itself — use delete_stack
    for that, after undeploying.

    Args:
        stack_name: Name or id of the stack to undeploy (required).
        services: Specific services to undeploy (empty undeploys all services).
        remove_orphans: Pass `--remove-orphans` to `docker compose down`.
        stop_time: Override the default termination max time.

    Returns the resulting Update status.
    """
    params: dict = {
        "stack": stack_name,
        "services": services or [],
        "remove_orphans": remove_orphans,
    }
    if stop_time is not None:
        params["stop_time"] = stop_time
    result = _make_request("DestroyStack", **params)
    if isinstance(result, dict) and "error" in result:
        return f"Error undeploying stack: {result['error']}"
    status = result.get("status", result) if isinstance(result, dict) else result
    return f"Stack '{stack_name}' undeploy (destroy) triggered.\nUpdate status: {status}"


@mcp.tool()
def delete_stack(stack_name: str = "") -> str:
    """
    Delete a Komodo Stack resource.

    This removes the Stack's configuration from Komodo. If containers
    are still running, undeploy_stack first.

    Args:
        stack_name: Name or id of the stack to delete (required).

    Returns the deleted stack's name.
    """
    result = _make_request("DeleteStack", id=stack_name)
    if isinstance(result, dict) and "error" in result:
        return f"Error deleting stack: {result['error']}"
    name = result.get("name", stack_name) if isinstance(result, dict) else stack_name
    return f"Stack '{name}' deleted."


@mcp.tool()
def list_actions() -> str:
    """
    List all Komodo Action resources and their descriptions.

    Returns a list of available actions with their types
    (read, execute, write) and descriptions.
    """
    result = _make_request("ListActions")
    if isinstance(result, dict) and "error" in result:
        return f"Error listing actions: {result['error']}"
    actions = _extract_list(result)
    if not actions:
        return "No actions available."
    return "Available actions:\n" + "\n".join(
        f"  - {a.get('name', '?')} ({a.get('type', '?')}): "
        f"{a.get('description', '')}"
        for a in actions
    )


@mcp.tool()
def get_action(action_name: str = "") -> str:
    """
    Get details about a specific Komodo Action resource.

    Args:
        action_name: Name or id of the action (required).

    Returns action details including status, parameters, and result.
    """
    result = _make_request("GetAction", action=action_name)
    if isinstance(result, dict) and "error" in result:
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

    # Komodo's /read/{variant} router only accepts POST — a GET always
    # 405s regardless of API health, so probe it the same way real
    # clients do.
    result = _make_request("ListStacks")
    if isinstance(result, dict) and "error" in result:
        details["komodo_api"] = f"unhealthy ({result['error']})"
        healthy = False
    else:
        details["komodo_api"] = "healthy"

    return f"Health: {'OK' if healthy else 'DEGRADED'}\n" + "\n".join(
        f"  {k}: {v}" for k, v in details.items()
    )


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
