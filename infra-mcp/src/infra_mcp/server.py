"""infra-mcp: Infrastructure MCP server for provisioning and managing services."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .github_auth import get_installation_token
from .scaffold import (
    scaffold_stateless_spec,
    scaffold_stateful_stub,
    scaffold_stateful_stack_block,
)

mcp = FastMCP(
    "infra-mcp",
    instructions=(
        "Provisions stateless (k3s) and stateful (Komodo) services. "
        "Stateless pattern: scaffold → provision → deploy_pr (k3s) + add_runner (arm64 CI). "
        "Stateful (Komodo) pattern: scaffold → provision_stateful (opens PR) → after merge, "
        "register_webhook (stack has no live deploy trigger until this runs — see "
        "komodo-dean-gitops/GITOPS_POLICY.md rule 5). "
        "Never hardcode secrets — use bws_name references in TOML specs."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/app/create", methods=["POST"])
async def app_create(request: Request) -> JSONResponse:
    """Full new-app provisioning: scaffold → provision → deploy PR + runner PR.

    Called by praetor webhook-adapter after GitHub repo creation. Runs all four
    infra-mcp steps sequentially and returns structured results for each step.
    """
    import asyncio

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    description = body.get("description") or f"{name} service"
    domain = body.get("domain")
    port = int(body.get("port", 8000))
    has_database = bool(body.get("has_database", False))

    results: dict = {"name": name}

    scaffold_result = await asyncio.to_thread(
        scaffold, name, description, domain, "stateless", port, has_database
    )
    results["scaffold"] = scaffold_result
    if "error" in scaffold_result:
        return JSONResponse(
            {"error": f"scaffold failed: {scaffold_result['error']}", **results},
            status_code=500,
        )

    provision_result = await asyncio.to_thread(provision, name)
    results["provision"] = provision_result
    if "error" in provision_result:
        return JSONResponse(
            {"error": f"provision failed: {provision_result['error']}", **results},
            status_code=500,
        )

    deploy_result = await asyncio.to_thread(deploy_pr, name, f"deploy: provision {name}")
    results["deploy_pr"] = deploy_result
    if "error" in deploy_result:
        results["warnings"] = [f"deploy PR failed: {deploy_result['error']}"]

    runner_result = await asyncio.to_thread(add_runner, name)
    results["runner_pr"] = runner_result
    if "error" in runner_result:
        results.setdefault("warnings", []).append(f"runner PR failed: {runner_result['error']}")

    return JSONResponse(results)


APP_FACTORY_DIR = Path(os.environ.get("APP_FACTORY_DIR", "/home/alex/claude/projects/app-factory"))
GITOPS_DIR = Path(os.environ.get("GITOPS_DIR", "/home/alex/claude/projects/k3s-dean-gitops"))
KOMODO_DIR = Path(os.environ.get("KOMODO_DIR", "/home/alex/claude/projects/komodo-dean-gitops"))

_STATEFUL_KEYWORDS = {
    "gpu", "local storage", "persistent", "stateful", "docker volume",
    "mac mini", "komodo", "hardware", "usb", "serial", "nvme", "nvidia",
}

_INLINE_SECRET_RE = re.compile(
    r'^\s*value\s*=\s*"[^"]{8,}"',
    re.MULTILINE,
)
_SECRET_WORD_RE = re.compile(
    r"(password|passwd|secret|token|api.?key|private.?key)",
    re.IGNORECASE,
)


def _infer_type(description: str) -> Literal["stateless", "stateful"]:
    desc = description.lower()
    return "stateful" if any(kw in desc for kw in _STATEFUL_KEYWORDS) else "stateless"


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    extra_env: dict | None = None,
    timeout: int = 300,
) -> tuple[int, str, str]:
    env = {**os.environ, **(extra_env or {})}
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _hygiene_violations(content: str) -> list[str]:
    violations = []
    for m in _INLINE_SECRET_RE.finditer(content):
        line = m.group(0).strip()
        if _SECRET_WORD_RE.search(line):
            violations.append(line)
    return violations


def _short_ts() -> str:
    return str(int(time.time()))[-6:]


@mcp.tool()
def scaffold(
    name: str,
    description: str,
    domain: str | None = None,
    app_type: str = "auto",
    port: int = 8000,
    has_database: bool = False,
) -> dict:
    """Generate a TOML app spec and write it to app-factory/apps/<name>.toml.

    For stateless apps (k3s), writes the spec file. For stateful apps (Komodo),
    returns a compose service stub to be added manually to the appropriate stack.

    Args:
        name: App name (lowercase, hyphens ok).
        description: What the service does — infers stateless vs stateful if app_type='auto'.
        domain: Public domain (default: <name>.amer.dev).
        app_type: 'stateless', 'stateful', or 'auto' (infer from description).
        port: Container port the service listens on.
        has_database: Include PostgreSQL database provisioning in the spec.
    """
    resolved_type = app_type if app_type in ("stateless", "stateful") else _infer_type(description)
    resolved_domain = domain or f"{name}.amer.dev"

    if resolved_type == "stateless":
        content = scaffold_stateless_spec(name, resolved_domain, port, has_database)
        spec_path = APP_FACTORY_DIR / "apps" / f"{name}.toml"
        spec_path.write_text(content)
        return {"path": str(spec_path), "app_type": "stateless", "next_step": f"provision('{name}')"}

    return {
        "app_type": "stateful",
        "next_step": (
            f"Call provision_stateful('{name}', description, server=..., port={port}) "
            "to write the compose file + [[stack]] block and open a PR. "
            f"After that PR is merged, call register_webhook('{name}') — a stack has no live "
            "deploy trigger until its GitHub webhook is registered (see GITOPS_POLICY.md rule 5)."
        ),
    }


@mcp.tool()
def provision(name: str) -> dict:
    """Provision a stateless app: validate TOML, run Tofu (secrets + DB), generate k8s manifests.

    Requires BWS_ACCESS_TOKEN env var. Reads spec from app-factory/apps/<name>.toml.
    Returns paths to all generated manifest files on success.
    """
    spec_path = APP_FACTORY_DIR / "apps" / f"{name}.toml"
    if not spec_path.exists():
        return {"error": f"Spec not found: {spec_path}. Run scaffold_app('{name}', ...) first."}

    violations = _hygiene_violations(spec_path.read_text())
    if violations:
        return {
            "error": "Inline secrets detected in TOML spec — refusing to provision.",
            "violations": violations,
            "fix": "Replace value = '...' secret fields with bws_name + generate references.",
        }

    bws_token = os.environ.get("BWS_ACCESS_TOKEN", "")
    if not bws_token:
        return {"error": "BWS_ACCESS_TOKEN not set — required to provision secrets and databases."}

    rc, stdout, stderr = _run(
        ["make", "create-app", f"APP={name}", f"GITOPS_DIR={GITOPS_DIR}"],
        cwd=APP_FACTORY_DIR,
        extra_env={"BWS_ACCESS_TOKEN": bws_token},
        timeout=300,
    )
    if rc != 0:
        return {"error": "make create-app failed", "stdout": stdout[-2000:], "stderr": stderr[-2000:]}

    generated = [str(p) for p in (GITOPS_DIR / "apps" / name).rglob("*.yaml")]
    return {"status": "provisioned", "generated_files": generated, "output": stdout[-1000:]}


@mcp.tool()
def deploy_pr(name: str, title: str) -> dict:
    """Commit generated manifests to k3s-dean-gitops and open a prod deploy PR.

    Uses the amerenda-coder GitHub App. Call provision(name) first.
    UAT manifests are committed directly to main (auto-deployed by ArgoCD).
    This PR gates prod deployment — requires human approval before merge.

    Returns the PR URL.
    """
    app_id = os.environ.get("CODER_APP_ID", "")
    installation_id = os.environ.get("CODER_INSTALLATION_ID", "")
    private_key = os.environ.get("CODER_APP_PRIVATE_KEY", "")
    if not all([app_id, installation_id, private_key]):
        return {"error": "CODER_APP_ID, CODER_INSTALLATION_ID, CODER_APP_PRIVATE_KEY must all be set."}

    try:
        token = get_installation_token(app_id, installation_id, private_key)
    except Exception as e:
        return {"error": f"GitHub App auth failed: {e}"}

    remote_url = f"https://x-access-token:{token}@github.com/amerenda/k3s-dean-gitops.git"
    branch = f"deploy/{name}-{_short_ts()}"
    bot_env = {
        "GIT_AUTHOR_NAME": "amerenda-coder[bot]",
        "GIT_AUTHOR_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "amerenda-coder[bot]",
        "GIT_COMMITTER_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
    }

    # Stage all paths generated by provision for this app.
    paths_to_stage: list[str] = [f"apps/{name}/", "root-app.yaml"]
    runner_dir = GITOPS_DIR / "infra" / f"arc-runners-{name}"
    if runner_dir.exists():
        paths_to_stage.append(f"infra/arc-runners-{name}/")
    appset_file = GITOPS_DIR / "infra" / "argocd-config" / "uat-applicationset.yaml"
    if appset_file.exists():
        paths_to_stage.append("infra/argocd-config/uat-applicationset.yaml")

    for cmd, desc in [
        (["git", "checkout", "-b", branch], "create branch"),
        (["git", "add"] + paths_to_stage, "stage manifests"),
        (["git", "commit", "-m", f"deploy: provision {name}\n\nGenerated by infra-mcp."], "commit"),
        (["git", "push", remote_url, branch], "push branch"),
    ]:
        rc, stdout, stderr = _run(cmd, cwd=GITOPS_DIR, extra_env=bot_env)
        if rc != 0:
            _run(["git", "checkout", "main"], cwd=GITOPS_DIR)
            return {"error": f"git {desc} failed", "stderr": stderr[-500:]}

    pr_resp = httpx.post(
        "https://api.github.com/repos/amerenda/k3s-dean-gitops/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": title,
            "head": branch,
            "base": "main",
            "body": (
                f"Provisioned by infra-mcp for `{name}`.\n\n"
                "Review generated manifests before merging to deploy to prod.\n"
                "UAT is already live — this PR gates production deployment."
            ),
        },
        timeout=30,
    )
    _run(["git", "checkout", "main"], cwd=GITOPS_DIR)

    if pr_resp.status_code not in (200, 201):
        return {"error": "Failed to open PR", "response": pr_resp.text[:500]}

    return {"status": "pr_opened", "url": pr_resp.json()["html_url"], "branch": branch}


_RUNNERS_COMPOSE = Path("mac-mini-m4/runners/compose.yaml")

_MAC_MINI_RUNNER_TEMPLATE = """\

  runner-{name}:
    <<: *runner-common
    container_name: runner-{name}
    environment:
      REPO_URL: https://github.com/amerenda/{name}
      RUNNER_NAME: mini-{name}
      LABELS: self-hosted,linux,arm64,docker,mac-mini
"""


@mcp.tool()
def add_runner(name: str, title: str | None = None) -> dict:
    """Add an arm64 GitHub Actions runner for an app on the mac-mini-m4 (via komodo-dean-gitops).

    Appends a runner service block to mac-mini-m4/runners/compose.yaml and opens a PR.
    The runner registers with the GitHub repo amerenda/<name> and joins the
    [self-hosted, linux, arm64, docker, mac-mini] runner group.

    Komodo auto-deploys within 60 seconds of the PR being merged.

    Args:
        name: App name (must match GitHub repo name under amerenda/).
        title: Optional PR title override.
    """
    app_id = os.environ.get("CODER_APP_ID", "")
    installation_id = os.environ.get("CODER_INSTALLATION_ID", "")
    private_key = os.environ.get("CODER_APP_PRIVATE_KEY", "")
    if not all([app_id, installation_id, private_key]):
        return {"error": "CODER_APP_ID, CODER_INSTALLATION_ID, CODER_APP_PRIVATE_KEY must all be set."}

    try:
        token = get_installation_token(app_id, installation_id, private_key)
    except Exception as e:
        return {"error": f"GitHub App auth failed: {e}"}

    compose_path = KOMODO_DIR / _RUNNERS_COMPOSE
    if not compose_path.exists():
        return {"error": f"Runners compose file not found: {compose_path}"}

    current = compose_path.read_text()

    # Idempotency: don't add if service block already exists.
    if f"runner-{name}:" in current:
        return {"status": "already_exists", "message": f"runner-{name} already in runners compose"}

    new_block = _MAC_MINI_RUNNER_TEMPLATE.format(name=name)
    updated = current.rstrip("\n") + "\n" + new_block
    compose_path.write_text(updated)

    remote_url = f"https://x-access-token:{token}@github.com/amerenda/komodo-dean-gitops.git"
    branch = f"feat/runner-{name}-{_short_ts()}"
    bot_env = {
        "GIT_AUTHOR_NAME": "amerenda-coder[bot]",
        "GIT_AUTHOR_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "amerenda-coder[bot]",
        "GIT_COMMITTER_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
    }
    pr_title = title or f"feat(runners): add mac-mini arm64 runner for {name}"

    for cmd, desc in [
        (["git", "checkout", "-b", branch], "create branch"),
        (["git", "add", str(_RUNNERS_COMPOSE)], "stage compose"),
        (["git", "commit", "-m", f"feat(runners): add mac-mini arm64 runner for {name}\n\nAdded by infra-mcp."], "commit"),
        (["git", "push", remote_url, branch], "push branch"),
    ]:
        rc, stdout, stderr = _run(cmd, cwd=KOMODO_DIR, extra_env=bot_env)
        if rc != 0:
            compose_path.write_text(current)  # revert the file change
            _run(["git", "checkout", "main"], cwd=KOMODO_DIR)
            return {"error": f"git {desc} failed", "stderr": stderr[-500:]}

    pr_resp = httpx.post(
        "https://api.github.com/repos/amerenda/komodo-dean-gitops/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": pr_title,
            "head": branch,
            "base": "main",
            "body": (
                f"Adds arm64 GitHub Actions runner for `{name}` on mac-mini-m4.\n\n"
                f"Runner name: `mini-{name}`\n"
                f"Labels: `self-hosted, linux, arm64, docker, mac-mini`\n\n"
                "Komodo auto-deploys within 60s of merge. "
                "The runner will register with `amerenda/{name}` on startup."
            ),
        },
        timeout=30,
    )
    _run(["git", "checkout", "main"], cwd=KOMODO_DIR)

    if pr_resp.status_code not in (200, 201):
        return {"error": "Failed to open PR", "response": pr_resp.text[:500]}

    return {"status": "pr_opened", "url": pr_resp.json()["html_url"], "branch": branch}


KOMODO_BASE = os.environ.get("KOMODO_BASE", "http://localhost:9120")
KOMODO_API_KEY = os.environ.get("KOMODO_API_KEY", "")
KOMODO_API_SECRET = os.environ.get("KOMODO_API_SECRET", "")
_KOMODO_SYNC_NAME = "komodo-dean-gitops"

# Same three-router shape verified working in komodo-mcp (read/write/execute,
# bare params dict as POST body, no JSON-RPC envelope) -- reuse it exactly
# rather than infra-mcp's older app_status()/KOMODO_URL GET-based call,
# which hits a different, unverified endpoint shape.
_KOMODO_METHODS = {"GetStack": "read", "RunSync": "execute"}


def _komodo_request(action: str, **params) -> dict:
    router = _KOMODO_METHODS[action]
    headers = {"Content-Type": "application/json"}
    if KOMODO_API_KEY:
        headers["X-Api-Key"] = KOMODO_API_KEY
    if KOMODO_API_SECRET:
        headers["X-Api-Secret"] = KOMODO_API_SECRET
    try:
        resp = httpx.post(
            f"{KOMODO_BASE}/{router}/{action}",
            json=params,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data
    except httpx.HTTPStatusError as exc:
        return {"error": f"{exc.response.status_code}: {exc.response.text[:300]}"}
    except Exception as exc:
        return {"error": str(exc)}


_GITHUB_HOOKS_URL = "https://api.github.com/repos/amerenda/komodo-dean-gitops/hooks"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@mcp.tool()
def provision_stateful(
    name: str,
    description: str,
    server: Literal["mac-mini-m4", "murderbot", "archlinux"] = "mac-mini-m4",
    port: int = 8000,
    title: str | None = None,
) -> dict:
    """Scaffold a new Komodo-managed (stateful) app and open a PR.

    Writes <server>/<name>/compose.yaml and appends a [[stack]] block to
    resource-sync/stacks.toml, with webhook_force_deploy = true always
    included (GITOPS_POLICY.md rule 5 — CI fails a PR missing it).

    This PR alone is not enough for the stack to actually deploy on future
    merges: after it's merged, call register_webhook(name). The GitHub
    deploy webhook can't be created before that, because its URL needs the
    stack's Komodo-assigned UUID, which only exists once ResourceSync has
    picked up the merged [[stack]] block.
    """
    app_id = os.environ.get("CODER_APP_ID", "")
    installation_id = os.environ.get("CODER_INSTALLATION_ID", "")
    private_key = os.environ.get("CODER_APP_PRIVATE_KEY", "")
    if not all([app_id, installation_id, private_key]):
        return {"error": "CODER_APP_ID, CODER_INSTALLATION_ID, CODER_APP_PRIVATE_KEY must all be set."}

    try:
        token = get_installation_token(app_id, installation_id, private_key)
    except Exception as e:
        return {"error": f"GitHub App auth failed: {e}"}

    compose_path = KOMODO_DIR / server / name / "compose.yaml"
    stacks_toml_path = KOMODO_DIR / "resource-sync" / "stacks.toml"

    if compose_path.exists():
        return {"error": f"{compose_path} already exists — refusing to overwrite."}
    stacks_toml_current = stacks_toml_path.read_text()
    if f'name = "{name}"' in stacks_toml_current:
        return {"error": f"A [[stack]] named '{name}' already exists in resource-sync/stacks.toml."}

    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(scaffold_stateful_stub(name, port))
    stack_block = scaffold_stateful_stack_block(name, description, server)
    stacks_toml_path.write_text(stacks_toml_current.rstrip("\n") + "\n\n" + stack_block)

    remote_url = f"https://x-access-token:{token}@github.com/amerenda/komodo-dean-gitops.git"
    branch = f"feat/stack-{name}-{_short_ts()}"
    bot_env = {
        "GIT_AUTHOR_NAME": "amerenda-coder[bot]",
        "GIT_AUTHOR_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "amerenda-coder[bot]",
        "GIT_COMMITTER_EMAIL": "amerenda-coder[bot]@users.noreply.github.com",
    }
    pr_title = title or f"feat({name}): provision new Komodo stack"
    rel_compose = str(compose_path.relative_to(KOMODO_DIR))

    def _revert():
        compose_path.unlink(missing_ok=True)
        stacks_toml_path.write_text(stacks_toml_current)
        _run(["git", "checkout", "main"], cwd=KOMODO_DIR)

    for cmd, desc in [
        (["git", "checkout", "-b", branch], "create branch"),
        (["git", "add", rel_compose, "resource-sync/stacks.toml"], "stage files"),
        (
            ["git", "commit", "-m", f"feat({name}): provision new Komodo stack\n\n"
             f"Added by infra-mcp. Run register_webhook('{name}') after merge."],
            "commit",
        ),
        (["git", "push", remote_url, branch], "push branch"),
    ]:
        rc, stdout, stderr = _run(cmd, cwd=KOMODO_DIR, extra_env=bot_env)
        if rc != 0:
            _revert()
            return {"error": f"git {desc} failed", "stderr": stderr[-500:]}

    pr_resp = httpx.post(
        "https://api.github.com/repos/amerenda/komodo-dean-gitops/pulls",
        headers=_github_headers(token),
        json={
            "title": pr_title,
            "head": branch,
            "base": "main",
            "body": (
                f"Provisions new Komodo stack `{name}` on `{server}`.\n\n"
                f"**After merge, run `register_webhook('{name}')`** — this stack has no live "
                "deploy trigger until its GitHub webhook is registered (GITOPS_POLICY.md rule 5)."
            ),
        },
        timeout=30,
    )
    _run(["git", "checkout", "main"], cwd=KOMODO_DIR)

    if pr_resp.status_code not in (200, 201):
        return {"error": "Failed to open PR", "response": pr_resp.text[:500]}

    return {
        "status": "pr_opened",
        "url": pr_resp.json()["html_url"],
        "branch": branch,
        "next_step": f"After merge, call register_webhook('{name}') to finish onboarding.",
    }


@mcp.tool()
def register_webhook(name: str, max_wait_seconds: int = 60) -> dict:
    """Register a GitHub deploy webhook for a Komodo stack so merges actually deploy it.

    Call after merging a provision_stateful() PR, or for any existing stack
    found missing its webhook (GITOPS_POLICY.md rule 5 — CI only checks
    webhook_force_deploy, not webhook existence, so a hand-added stack can
    still be silently orphaned; verify with
    `gh api repos/amerenda/komodo-dean-gitops/hooks`).

    Triggers RunSync first — Komodo's ResourceSync webhook is confirmed
    broken (moghtech/komodo#1120) and never auto-executes, so a merged
    [[stack]] block otherwise wouldn't be visible to the Komodo API until
    the 5-minute poll fallback runs. Then polls GetStack for the new
    stack's UUID, and creates the webhook via the GitHub API using
    komodo-dean-webhook-secret from BWS. Idempotent — a stack that already
    has this exact webhook registered returns status=already_exists.

    Requires KOMODO_API_KEY, a GitHub App token (CODER_APP_ID/
    CODER_INSTALLATION_ID/CODER_APP_PRIVATE_KEY), and BWS_ACCESS_TOKEN.
    """
    if not KOMODO_API_KEY:
        return {"error": "KOMODO_API_KEY not set."}

    app_id = os.environ.get("CODER_APP_ID", "")
    installation_id = os.environ.get("CODER_INSTALLATION_ID", "")
    private_key = os.environ.get("CODER_APP_PRIVATE_KEY", "")
    if not all([app_id, installation_id, private_key]):
        return {"error": "CODER_APP_ID, CODER_INSTALLATION_ID, CODER_APP_PRIVATE_KEY must all be set."}
    try:
        token = get_installation_token(app_id, installation_id, private_key)
    except Exception as e:
        return {"error": f"GitHub App auth failed: {e}"}

    sync_result = _komodo_request("RunSync", sync=_KOMODO_SYNC_NAME)
    if isinstance(sync_result, dict) and "error" in sync_result:
        return {"error": f"RunSync failed: {sync_result['error']}"}

    stack = None
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        result = _komodo_request("GetStack", name=name)
        if isinstance(result, dict) and "_id" in result:
            stack = result
            break
        time.sleep(3)
    if stack is None:
        return {"error": f"Stack '{name}' not found in Komodo after RunSync + {max_wait_seconds}s wait."}

    raw_id = stack["_id"]
    stack_id = raw_id.get("$oid") if isinstance(raw_id, dict) else raw_id
    if not stack_id:
        return {"error": f"Could not determine stack id from GetStack response: {stack}"}

    webhook_url = f"https://pubhooks.amer.dev/listener/github/stack/{stack_id}/deploy"
    existing_resp = httpx.get(_GITHUB_HOOKS_URL, headers=_github_headers(token), timeout=30)
    if existing_resp.status_code == 200:
        for hook in existing_resp.json():
            if hook.get("config", {}).get("url") == webhook_url:
                return {"status": "already_exists", "stack_id": stack_id, "webhook_id": hook["id"]}

    secret_resp = resolve_secret("komodo-dean-webhook-secret")
    if "error" in secret_resp:
        return {"error": f"Could not resolve komodo-dean-webhook-secret: {secret_resp['error']}"}

    hook_resp = httpx.post(
        _GITHUB_HOOKS_URL,
        headers=_github_headers(token),
        json={
            "name": "web",
            "active": True,
            "events": ["push"],
            "config": {
                "url": webhook_url,
                "content_type": "json",
                "secret": secret_resp["value"],
                "insecure_ssl": "0",
            },
        },
        timeout=30,
    )
    if hook_resp.status_code not in (200, 201):
        return {"error": "Failed to create webhook", "response": hook_resp.text[:500]}

    return {
        "status": "webhook_registered",
        "stack_id": stack_id,
        "webhook_id": hook_resp.json()["id"],
        "url": webhook_url,
    }


@mcp.tool()
def check_secrets(
    repo: Literal["k3s-dean-gitops", "komodo-dean-gitops", "app-factory"],
) -> dict:
    """Scan a gitops repo for hardcoded secrets in committed YAML/TOML/env files.

    Flags patterns like `password:`, `token:`, `api_key:` with non-reference values.
    Returns findings with file:line references; clean=True means no issues found.
    """
    repo_dirs = {
        "k3s-dean-gitops": GITOPS_DIR,
        "komodo-dean-gitops": KOMODO_DIR,
        "app-factory": APP_FACTORY_DIR,
    }
    repo_dir = repo_dirs[repo]
    patterns = [
        r"password\s*[:=]",
        r"passwd\s*[:=]",
        r"secret\s*[:=]",
        r"api.?key\s*[:=]",
        r"private.?key\s*[:=]",
        r"AWS_SECRET",
        r"AWS_ACCESS_KEY",
    ]
    findings = []
    for pattern in patterns:
        rc, stdout, _ = _run(
            ["git", "grep", "-inE", pattern, "--", "*.yaml", "*.toml", "*.env", "*.json"],
            cwd=repo_dir,
        )
        if rc == 0:
            for line in stdout.strip().splitlines():
                # Skip known-safe ExternalSecret boilerplate and bws references
                if any(safe in line for safe in ("secretKeyRef", "secretName", "bws_name", "bws_key", "ExternalSecret", "k8s_secret")):
                    continue
                findings.append(line)

    return {"repo": repo, "clean": len(findings) == 0, "findings": findings}


@mcp.tool()
def app_status(
    name: str,
    app_type: Literal["stateless", "stateful"] = "stateless",
) -> dict:
    """Get deployment status for an app from ArgoCD (stateless) or Komodo (stateful).

    For stateless apps, returns ArgoCD sync/health status and pod state.
    For stateful apps, queries the Komodo API (requires KOMODO_API_KEY env var).
    """
    if app_type == "stateless":
        rc, stdout, _ = _run([
            "kubectl", "get", "applications", name, "-n", "default",
            "-o", "jsonpath={.status.sync.status},{.status.health.status}",
        ])
        pods_rc, pods_out, _ = _run(["kubectl", "get", "pods", "-n", name, "--no-headers"])
        sync, health = ("unknown", "unknown")
        if rc == 0 and stdout:
            parts = stdout.split(",")
            sync = parts[0] if parts else "unknown"
            health = parts[1] if len(parts) > 1 else "unknown"
        return {"app": name, "sync": sync, "health": health, "pods": pods_out.strip()}

    komodo_url = os.environ.get("KOMODO_URL", "https://komodo.amer.dev")
    komodo_key = os.environ.get("KOMODO_API_KEY", "")
    if not komodo_key:
        return {"app": name, "error": "KOMODO_API_KEY not set — cannot query Komodo API."}
    try:
        resp = httpx.get(
            f"{komodo_url}/api/stack/{name}",
            headers={"x-api-key": komodo_key},
            timeout=10,
        )
        return {"app": name, "komodo": resp.json()}
    except Exception as e:
        return {"app": name, "error": str(e)}


@mcp.tool()
def resolve_secret(secret_name: str) -> dict:
    """Look up a BWS secret value by its key name using the read-only service account token.

    Requires BWS_SERVICE_ACCOUNT_TOKEN env var.
    Returns the secret value for use in compose files or config — never stores it.
    """
    token = os.environ.get("BWS_SERVICE_ACCOUNT_TOKEN") or os.environ.get("BWS_ACCESS_TOKEN", "")
    if not token:
        return {"error": "BWS_ACCESS_TOKEN not set."}

    rc, stdout, stderr = _run(
        ["bws", "secret", "list", "--output", "json"],
        extra_env={"BWS_ACCESS_TOKEN": token},
    )
    if rc != 0:
        return {"error": f"bws list failed: {stderr[:500]}"}

    try:
        secrets = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "Failed to parse bws output", "raw": stdout[:300]}

    for s in secrets:
        if s.get("key") == secret_name:
            return {"key": secret_name, "value": s["value"]}
    return {"error": f"Secret '{secret_name}' not found in BWS."}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
