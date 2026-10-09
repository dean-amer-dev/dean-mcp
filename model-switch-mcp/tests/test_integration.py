"""Live test through the deployed MCP. Run with:
MSM_INTEGRATION=1 MSM_URL=https://model-switch-mcp.amer.dev/mcp MSM_TOKEN=... pytest tests/test_integration.py
It switches the real GPU: the default model is preempted for a few minutes."""
import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MSM_INTEGRATION") != "1", reason="needs a deployed MCP")

REPO, FILE = "Qwen/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q8_0.gguf"


async def call(client, name, **args):
    res = await client.call_tool(name, args, raise_on_error=False)
    text = res.content[0].text
    return json.loads(text)


def test_stage_switch_restore():
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def run():
        transport = StreamableHttpTransport(os.environ["MSM_URL"], headers={"Authorization": "Bearer " + os.environ["MSM_TOKEN"]})
        async with Client(transport) as c:
            tools = {t.name for t in await c.list_tools()}
            assert {"switch_model", "stage_model", "restore_default"} <= tools
            assert (await call(c, "list_engines"))["engines"].keys() >= {"ninfer", "llamacpp", "ollama"}
            staged = await call(c, "stage_model", engine="llamacpp", repo=REPO, file=FILE, wait_minutes=10)
            assert staged["state"] == "staged", staged
            out = await call(c, "switch_model", engine="llamacpp", repo=REPO, file=FILE, ctx=4096, lease_minutes=5)
            assert out["state"] == "ready", out
            restored = await call(c, "restore_default")
            assert restored["ok"]

    asyncio.run(run())
