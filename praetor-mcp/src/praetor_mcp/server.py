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
