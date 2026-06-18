"""praetor-mcp: dispatch Praetor agents from any MCP-aware client."""
from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

_PRAETOR_BASE = os.environ.get("PRAETOR_BASE_URL", "https://praetor.amer.dev").rstrip("/")
_PRAETOR_API_KEY = os.environ.get("PRAETOR_API_KEY", "")

mcp = FastMCP(
    "praetor-mcp",
    instructions=(
        "Dispatch and monitor Praetor AI agent tasks. "
        "Use dispatch_praetor_task to start research, code, or pipeline agents. "
        "Use get_praetor_status to check completion. "
        "For code/pipeline tasks, include 'repo: owner/name' in the description."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_PRAETOR_API_KEY}"}


@mcp.tool()
def dispatch_praetor_task(title: str, description: str, task_type: str) -> str:
    """
    Dispatch a Praetor agent task and return its task_id.

    task_type must be one of: research | code | pipeline
    - research: web search + summarise, output written to Mem0
    - code: read a repo, implement changes, open a PR
    - pipeline: research first, then code (both labels)

    For code or pipeline tasks, include 'repo: owner/name' in description.

    Returns a confirmation string with the task_id. Use get_praetor_status to check completion.
    """
    if not _PRAETOR_API_KEY:
        return "Error: PRAETOR_API_KEY not configured on this MCP server."
    if task_type not in ("research", "code", "pipeline"):
        return f"Error: task_type must be research, code, or pipeline — got '{task_type}'"
    try:
        resp = httpx.post(
            f"{_PRAETOR_BASE}/api/v1/dispatch",
            json={"title": title, "description": description, "type": task_type},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            f"Dispatched {data['event']} — task_id={data['task_id']}. "
            f"View run at {data['hatchet_url']}"
        )
    except httpx.HTTPStatusError as exc:
        return f"Error: dispatch failed ({exc.response.status_code}): {exc.response.text[:200]}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def create_app(
    name: str,
    description: str,
    port: int = 8000,
    domain: str | None = None,
    has_database: bool = False,
    env_secrets: dict | None = None,
) -> str:
    """
    Full app pipeline: create a new GitHub repo, provision k3s manifests, dispatch coder.

    IMPORTANT: Always present a structured plan and wait for explicit user approval
    before calling this tool. Never call create_app speculatively or without approval.

    What this does:
    1. Creates a GitHub repo at amerenda/<name> from the app-template skeleton
    2. Provisions UAT + prod k3s manifests via infra-mcp (ArgoCD-ready before first deploy)
    3. Adds an arm64 CI runner on mac-mini for the new repo
    4. Dispatches the coder agent to write the initial implementation and open a PR

    After the coder's PR is merged, CI builds multi-arch images and ArgoCD auto-syncs UAT.
    A prod deploy PR is created separately for human approval.

    Args:
        name: App name — lowercase kebab-case (e.g. "my-svc"). Becomes the GitHub repo name.
        description: What the app does — used as the coder agent's implementation brief.
        port: Container port (default: 8000).
        domain: Public domain (default: <name>.amer.dev).
        has_database: Include PostgreSQL provisioning (default: False).
        env_secrets: {ENV_VAR: bws-secret-name} for secrets the app needs.

    Returns repo URL and task_id for tracking the coder agent via get_praetor_status.
    """
    if not _PRAETOR_API_KEY:
        return "Error: PRAETOR_API_KEY not configured on this MCP server."

    plan: dict = {
        "name": name,
        "description": description,
        "port": port,
        "has_database": has_database,
        "stateless": True,
    }
    if domain is not None:
        plan["domain"] = domain
    if env_secrets:
        plan["env_secrets"] = env_secrets

    try:
        resp = httpx.post(
            f"{_PRAETOR_BASE}/api/v1/app/create",
            json=plan,
            headers=_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            f"App pipeline started!\n"
            f"Repo: {data['repo_url']}\n"
            f"task_id: {data['task_id']}\n"
            f"{data['message']}"
        )
    except httpx.HTTPStatusError as exc:
        return f"Error: create_app failed ({exc.response.status_code}): {exc.response.text[:300]}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def request_mcp(capability: str, preferred_name: str | None = None) -> str:
    """
    Find or build an MCP server for a requested capability.

    IMPORTANT: Describe the capability clearly in natural language before calling this.
    The agent will research existing MCP servers and either register a found one or
    scaffold a new one from scratch.

    Args:
        capability: Natural language description of what the MCP server should do.
                    Example: "query Grafana alerts and datasources via the Grafana HTTP API"
        preferred_name: Optional kebab-case name for the MCP (e.g. "mcp-grafana").
                        If omitted, a name is derived from the capability or research results.

    Returns a decision ("already_registered", "use_existing", or "scaffold_new"),
    PR URL or task_id, and a research summary.
    """
    if not _PRAETOR_API_KEY:
        return "Error: PRAETOR_API_KEY not configured on this MCP server."

    body: dict = {"capability": capability}
    if preferred_name:
        body["preferred_name"] = preferred_name

    try:
        resp = httpx.post(
            f"{_PRAETOR_BASE}/api/v1/mcp/request",
            json=body,
            headers=_headers(),
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [
            f"Decision: {data['decision']}",
            f"Research: {data['research_summary']}",
        ]
        if data.get("pr_url"):
            parts.append(f"PR: {data['pr_url']}")
        if data.get("task_id"):
            parts.append(f"task_id: {data['task_id']} — track at https://hatchet.amer.dev")
        if data.get("image"):
            parts.append(f"Image: {data['image']}")
        parts.append(data["message"])
        return "\n".join(parts)
    except httpx.HTTPStatusError as exc:
        return f"Error: request_mcp failed ({exc.response.status_code}): {exc.response.text[:300]}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_praetor_status(task_id: int) -> str:
    """
    Check the status of a previously dispatched Praetor task.

    Returns whether the task is done and a summary from Mem0 if available.
    Call dispatch_praetor_task first to get a task_id.
    """
    if not _PRAETOR_API_KEY:
        return "Error: PRAETOR_API_KEY not configured on this MCP server."
    try:
        resp = httpx.get(
            f"{_PRAETOR_BASE}/api/v1/status/{task_id}",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["done"]:
            return f"Done. {data['mem0_summary']}"
        return "Still running. Check back in a minute or view at https://hatchet.amer.dev"
    except httpx.HTTPStatusError as exc:
        return f"Error: status check failed ({exc.response.status_code})"
    except Exception as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
