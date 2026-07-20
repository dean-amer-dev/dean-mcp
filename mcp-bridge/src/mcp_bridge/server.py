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

# LiteLLM's stateless POST /mcp/tools/list endpoint always returns zero tools —
# the gateway only serves tools over the stateful MCP session protocol: an
# `initialize` call against the root endpoint returns an `mcp-session-id`
# header, which must then be sent on every subsequent request.
MCP_ENDPOINT = LITELLM_MCP_URL.rstrip("/") + "/"

app = FastAPI(title="MCP Bridge", version="1.0.0", docs_url=None, redoc_url=None)
router = APIRouter(prefix="/mcp")

_tools: dict[str, dict] = {}
_lock = asyncio.Lock()

_session_id: str | None = None
_session_lock = asyncio.Lock()


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


def _headers(session_id: str | None) -> dict:
    headers = {
        "Authorization": f"Bearer {LITELLM_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


async def _initialize_session(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        MCP_ENDPOINT,
        headers=_headers(None),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-bridge", "version": "1.0.0"},
            },
        },
    )
    resp.raise_for_status()
    session_id = resp.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP initialize did not return mcp-session-id")
    return session_id


async def _mcp_call(client: httpx.AsyncClient, method: str, params: dict) -> dict:
    """Call the stateful MCP endpoint, initializing or re-initializing the session as needed."""
    global _session_id

    async with _session_lock:
        if _session_id is None:
            _session_id = await _initialize_session(client)
        session_id = _session_id

    resp = await client.post(
        MCP_ENDPOINT,
        headers=_headers(session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    )
    if resp.status_code in (400, 404):
        # Session likely expired — re-initialize once and retry.
        async with _session_lock:
            _session_id = await _initialize_session(client)
            session_id = _session_id
        resp = await client.post(
            MCP_ENDPOINT,
            headers=_headers(session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        )
    resp.raise_for_status()
    return _parse_sse(resp.text)


async def _refresh_tools() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        result = await _mcp_call(client, "tools/list", {})

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

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            result = await _mcp_call(
                client, "tools/call", {"name": tool_name, "arguments": body}
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # MCP tools/call returns {"content": [{"type": "text", "text": "..."}]}
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        text = content[0]["text"]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return JSONResponse({"result": text})

        # Many tools return {"content": "<file text>", ...metadata...} or
        # {"diff": "<patch>", ...metadata...}.  Returning the full dict causes
        # models to loop because they see metadata fields and don't recognise the
        # response as satisfying their tool call.  Surface the primary text field
        # directly so the model receives readable content.
        for key in ("content", "diff"):
            val = parsed.get(key)
            if isinstance(val, str) and len(val) > 20:
                out: dict = {key: val}
                if parsed.get("truncated"):
                    out["truncated"] = True
                return JSONResponse(out)

        return JSONResponse(parsed)

    return JSONResponse(result)


app.include_router(router)
