"""FastMCP server: tools over the switching logic, bearer-token auth, stateless HTTP so replicas need no affinity."""
import hmac
import json
import os
import threading
import time

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import validate
from .service import OperationFailed, Switch

CONFIG_NAMESPACE = os.environ.get("RUNNERS_NAMESPACE", "llm-bench")
CACHE_SECONDS = 30

mcp = FastMCP(
    name="model-switch-mcp",
    instructions=(
        "Switches a GPU runner between its default model and a benchmark model. Workflow: stage_model "
        "(downloads and validates weights on CPU, does not touch the GPU), poll get_status until the stage Job "
        "has ended, then switch_model (takes the GPU; the default model is preempted and returns by itself "
        "when the lease ends or restore_default is called). The runner argument defaults to murderbot."
    ),
)

_state = {"switch": None, "runners": None, "loaded": 0.0}
_lock = threading.Lock()


def _runners():
    with _lock:
        if _state["runners"] is None or time.time() - _state["loaded"] > CACHE_SECONDS:
            if os.environ.get("RUNNERS_FILE"):
                _state["runners"] = yaml.safe_load(open(os.environ["RUNNERS_FILE"]))["runners"]
            else:
                _state["runners"] = _kube().read_runners_yaml(CONFIG_NAMESPACE)["runners"]
            _state["loaded"] = time.time()
        return _state["runners"]


def _kube():
    if _state.get("kube") is None:
        from .kube import Kube
        _state["kube"] = Kube()
    return _state["kube"]


def _switch():
    if _state["switch"] is None:
        _state["switch"] = Switch(_kube(), _runners)
    return _state["switch"]


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except validate.ValidationError as e:
        raise ToolError(json.dumps(e.as_dict()))
    except OperationFailed as e:
        return e.result


@mcp.tool
def list_engines(runner: str = "murderbot") -> dict:
    """Engines enabled on a runner (name, pinned image, health path), lease and startup limits, default model."""
    return _run(lambda: _switch().list_engines(runner))


@mcp.tool
def stage_model(engine: str, repo: str, revision: str = "", file: str = "", ctx: int = 4096,
                wait_minutes: int = 0, runner: str = "murderbot") -> dict:
    """Download and validate a Hugging Face model into the runner's weights volume (CPU only, GPU untouched).

    engine: ninfer | llamacpp | ollama. repo: owner/name. revision: commit sha, tag or branch (resolved to a sha).
    file: file name or glob when the repo holds several models (GGUF quants); ninfer picks the single .ninfer file.
    ctx: context tokens used for the fit check. wait_minutes: block up to this long (max 30) for the result.
    Rejects models that cannot fit the GPU, unsupported container versions and bad headers before the GPU is used.
    """
    return _run(lambda: _switch().stage_model(runner, engine, repo, revision, file, ctx, wait_minutes))


@mcp.tool
def list_staged(runner: str = "murderbot") -> dict:
    """Models staged on the runner's weights volume, with size, engines they were validated for, last use, free space."""
    return _run(lambda: _switch().list_staged(runner))


@mcp.tool
def delete_staged(repo: str, revision: str, file: str, runner: str = "murderbot") -> dict:
    """Delete one staged model (exact repo, revision sha and primary file from list_staged). Refused while it is serving."""
    return _run(lambda: _switch().delete_staged(runner, repo, revision, file))


@mcp.tool
def get_status(runner: str = "murderbot") -> dict:
    """Serve and stage runs on the runner: phase, lease remaining, stage results, GPU quota. Also what the default model is."""
    return _run(lambda: _switch().get_status(runner))


@mcp.tool
def switch_model(engine: str, repo: str, revision: str = "", file: str = "", ctx: int = 4096,
                 args: list[str] | None = None, env: dict[str, str] | None = None,
                 lease_minutes: int = 0, startup_minutes: int = 0, wait: bool = True,
                 runner: str = "murderbot") -> dict:
    """Run a staged model on the GPU. Preempts the default model; it returns by itself when the lease ends.

    The model must be staged for this engine. args are appended verbatim to the engine command line (ninfer, llamacpp);
    env sets engine environment variables (the way to configure ollama). Flags that load other weights are rejected.
    lease_minutes (default 30, max 120) is the hard limit on the whole run including startup; startup_minutes
    (default 10, max 20) kills an engine that never becomes ready. Any earlier bench run is replaced.
    Returns ready with the in-cluster endpoint (served model id: bench), or the failure with log tail and events.
    """
    return _run(lambda: _switch().switch_model(runner, engine, repo, revision, file, ctx, args, env,
                                               lease_minutes, startup_minutes, wait))


@mcp.tool
def restore_default(runner: str = "murderbot") -> dict:
    """End any bench run now. The default model reschedules by itself (about a minute to load)."""
    return _run(lambda: _switch().restore_default(runner))


@mcp.tool
def get_logs(run_id: str = "", kind: str = "serve", tail: int = 200, container: str = "",
             runner: str = "murderbot") -> dict:
    """Log tail and events of the latest (or given) serve or stage run. container: engine | fetch | import."""
    return _run(lambda: _switch().get_logs(runner, run_id, kind, tail, container))


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] != "/health":
            header = dict(scope["headers"]).get(b"authorization", b"")
            supplied = header[7:] if header[:7].lower() == b"bearer " else b""
            if not hmac.compare_digest(supplied, self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"ok": True})


def build_app():
    token = os.environ.get("MCP_BEARER_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("MCP_BEARER_TOKEN must be set (at least 24 characters)")
    return mcp.http_app(path="/mcp", stateless_http=True, middleware=[Middleware(BearerAuth, token=token)])


def main():
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
