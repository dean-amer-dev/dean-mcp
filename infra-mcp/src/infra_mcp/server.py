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

from .github_auth import get_installation_token
from .scaffold import scaffold_stateless_spec, scaffold_stateful_stub

mcp = FastMCP(
    "infra-mcp",
    instructions=(
        "Provisions stateless (k3s) and stateful (Komodo) services. "
        "Pattern: scaffold_app → provision_app → open_deploy_pr. "
        "Never hardcode secrets — use bws_name references in TOML specs."
    ),
)

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
def scaffold_app(
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
        return {"path": str(spec_path), "app_type": "stateless", "next_step": f"provision_app('{name}')"}

    content = scaffold_stateful_stub(name, port)
    return {
        "app_type": "stateful",
        "compose_stub": content,
        "next_step": (
            f"1. Add the compose block to the appropriate komodo-dean-gitops/<host>/<stack>/compose.yaml. "
            f"2. If a database or generated secrets are needed, run provision_app('{name}'). "
            f"3. Commit and push to komodo-dean-gitops — Komodo auto-deploys within 60s."
        ),
    }


@mcp.tool()
def provision_app(name: str) -> dict:
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
def open_deploy_pr(name: str, title: str) -> dict:
    """Commit generated manifests to k3s-dean-gitops and open a prod deploy PR.

    Uses the amerenda-coder GitHub App. Call provision_app(name) first.
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

    for cmd, desc in [
        (["git", "checkout", "-b", branch], "create branch"),
        (["git", "add", f"apps/{name}/"], "stage manifests"),
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


@mcp.tool()
def check_secret_hygiene(
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
def get_app_status(
    name: str,
    app_type: Literal["stateless", "stateful"] = "stateless",
) -> dict:
    """Get deployment status for an app from ArgoCD (stateless) or Komodo (stateful).

    For stateless apps, returns ArgoCD sync/health status and pod state.
    For stateful apps, queries the Komodo API (requires KOMODO_API_KEY env var).
    """
    if app_type == "stateless":
        rc, stdout, _ = _run([
            "kubectl", "get", "applications", name, "-n", "argocd",
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
def resolve_secret_name(secret_name: str) -> dict:
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
    mcp.run()
