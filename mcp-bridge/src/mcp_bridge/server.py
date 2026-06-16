"""mcp-bridge: OpenAPI wrapper for LiteLLM MCP gateway tools.

Fetches tools from the MCP gateway on startup and exposes them as standard
REST endpoints under /mcp/tools/{name}, with a /mcp/openapi.json spec that
OpenWebUI Tool Servers can consume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LITELLM_MCP_URL = os.environ.get("LITELLM_MCP_URL", "https://litellm.amer.dev/mcp")
LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]

app = FastAPI(title="MCP Bridge", version="1.0.0", docs_url=None, redoc_url=None)
router = APIRouter(prefix="/mcp")

_tools: dict[str, dict] = {}
_lock = asyncio.Lock()


def _parse_sse(text: str) -> dict:
    """Extract the first JSON-RPC result from an SSE event stream."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if "result" in payload:
                return payload["result"]
            if "error" in payload:
                raise RuntimeError(payload["error"].get("message", "MCP error"))
    raise RuntimeError("No result found in MCP SSE response")


async def _refresh_tools() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{LITELLM_MCP_URL}/tools/list",
            headers={
                "Authorization": f"Bearer {LITELLM_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        resp.raise_for_status()

    result = _parse_sse(resp.text)
    async with _lock:
        _tools.clear()
        for tool in result.get("tools", []):
            _tools[tool["name"]] = tool

    logger.info("Loaded %d tools from MCP gateway", len(_tools))


@app.on_event("startup")
async def startup() -> None:
    await _refresh_tools()


def _build_spec() -> dict:
    paths: dict = {}
    for name, tool in _tools.items():
        paths[f"/tools/{name}"] = {
            "post": {
                "operationId": name,
                "summary": (tool.get("description") or name)[:200],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": tool.get("inputSchema", {"type": "object"})
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Tool result",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "MCP Bridge",
            "description": "OpenAPI wrapper for LiteLLM MCP gateway tools",
            "version": "1.0.0",
        },
        "servers": [{"url": "/mcp"}],
        "paths": paths,
    }


@router.get("/openapi.json", include_in_schema=False)
async def openapi_json() -> JSONResponse:
    # Re-fetch tools so newly registered MCP servers appear automatically.
    await _refresh_tools()
    return JSONResponse(_build_spec())


@router.post("/tools/{tool_name}")
async def call_tool(tool_name: str, request: Request) -> JSONResponse:
    async with _lock:
        if tool_name not in _tools:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    body = await request.json()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{LITELLM_MCP_URL}/tools/call",
            headers={
                "Authorization": f"Bearer {LITELLM_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": body},
            },
        )
        resp.raise_for_status()

    try:
        result = _parse_sse(resp.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # MCP tools/call returns {"content": [{"type": "text", "text": "..."}]}
    # Unwrap to plain JSON so OpenWebUI can parse the result.
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        text = content[0]["text"]
        try:
            return JSONResponse(json.loads(text))
        except json.JSONDecodeError:
            return JSONResponse({"result": text})

    return JSONResponse(result)


app.include_router(router)
