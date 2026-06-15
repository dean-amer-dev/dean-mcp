"""github-mcp: Read-only GitHub repo browsing for planning sessions."""
from __future__ import annotations

import base64
import os
import time

import httpx
import jwt
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

_APP_ID = os.environ["GITHUB_APP_ID"]
_PRIVATE_KEY = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n")
_INSTALLATION_ID = os.environ["GITHUB_APP_INSTALLATION_ID"]
_ORG = os.environ.get("GITHUB_ORG", "amerenda")
_MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", "32768"))

mcp = FastMCP(
    "github-mcp",
    instructions=(
        "Read-only GitHub repo browsing for the amerenda org. "
        "Use list_files to explore a repo directory, read_file to read a specific file "
        "(capped at 32KB), search_code for symbol/string searches, list_prs for open PRs, "
        "get_pr_diff to review a PR, and list_commits for recent history."
    ),
)

_gh_token_cache: tuple[str, float] | None = None


def _get_installation_token() -> str:
    global _gh_token_cache
    if _gh_token_cache and time.time() < _gh_token_cache[1] - 60:
        return _gh_token_cache[0]
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": _APP_ID}
    app_jwt = jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256")
    resp = httpx.post(
        f"https://api.github.com/app/installations/{_INSTALLATION_ID}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["token"]
    try:
        from datetime import datetime
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
    except Exception:
        exp = time.time() + 3600
    _gh_token_cache = (token, exp)
    return token


def _gh(path: str, accept: str = "application/vnd.github.v3+json") -> httpx.Response:
    return httpx.get(
        f"https://api.github.com{path}",
        headers={"Authorization": f"token {_get_installation_token()}", "Accept": accept},
        timeout=15,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool()
def list_files(repo: str, path: str = "", ref: str = "main") -> dict:
    """List files and directories at a path in an amerenda org repo.

    Args:
        repo: Repository name (without the org prefix), e.g. 'praetor'.
        path: Directory path inside the repo, e.g. 'agents/coder'. Empty string = repo root.
        ref: Git ref (branch, tag, or SHA). Defaults to 'main'.
    """
    url = f"/repos/{_ORG}/{repo}/contents/{path}"
    if ref != "main":
        url += f"?ref={ref}"
    resp = _gh(url)
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    items = resp.json()
    if isinstance(items, list):
        return {
            "repo": repo,
            "path": path,
            "entries": [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items],
        }
    return {"repo": repo, "path": path, "type": "file", "size": items.get("size")}


@mcp.tool()
def read_file(repo: str, path: str, ref: str = "main") -> dict:
    """Read a file from an amerenda org repo (response capped at 32KB).

    Args:
        repo: Repository name, e.g. 'praetor'.
        path: File path inside the repo, e.g. 'agents/coder/agent.py'.
        ref: Git ref. Defaults to 'main'.
    """
    url = f"/repos/{_ORG}/{repo}/contents/{path}"
    if ref != "main":
        url += f"?ref={ref}"
    resp = _gh(url)
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    data = resp.json()
    if data.get("encoding") == "base64":
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    else:
        content = data.get("content", "")
    truncated = len(content) > _MAX_FILE_CHARS
    return {
        "repo": repo,
        "path": path,
        "content": content[:_MAX_FILE_CHARS],
        "truncated": truncated,
        "size": data.get("size"),
    }


@mcp.tool()
def search_code(repo: str, query: str) -> dict:
    """Search for code in an amerenda org repo using GitHub code search.

    Args:
        repo: Repository name, e.g. 'praetor'.
        query: Search query, e.g. 'def build_agent' or 'hatchet.task'.
    """
    resp = _gh(f"/search/code?q={query}+repo:{_ORG}/{repo}&per_page=10")
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    items = resp.json().get("items", [])
    return {
        "count": len(items),
        "results": [{"path": i["path"], "url": i["html_url"]} for i in items],
    }


@mcp.tool()
def list_prs(repo: str, state: str = "open") -> dict:
    """List pull requests in an amerenda org repo.

    Args:
        repo: Repository name, e.g. 'praetor'.
        state: 'open', 'closed', or 'all'. Defaults to 'open'.
    """
    resp = _gh(f"/repos/{_ORG}/{repo}/pulls?state={state}&per_page=20")
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    prs = resp.json()
    return {
        "count": len(prs),
        "prs": [
            {
                "number": p["number"],
                "title": p["title"],
                "author": p["user"]["login"],
                "branch": p["head"]["ref"],
                "updated_at": p["updated_at"],
            }
            for p in prs
        ],
    }


@mcp.tool()
def get_pr_diff(repo: str, pr_number: int) -> dict:
    """Fetch the unified diff for a pull request (capped at 32KB).

    Args:
        repo: Repository name, e.g. 'praetor'.
        pr_number: Pull request number.
    """
    resp = _gh(
        f"/repos/{_ORG}/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    diff = resp.text
    truncated = len(diff) > _MAX_FILE_CHARS
    return {"repo": repo, "pr": pr_number, "diff": diff[:_MAX_FILE_CHARS], "truncated": truncated}


@mcp.tool()
def list_commits(repo: str, branch: str = "main", limit: int = 10) -> dict:
    """List recent commits on a branch.

    Args:
        repo: Repository name, e.g. 'praetor'.
        branch: Branch name. Defaults to 'main'.
        limit: Number of commits to return (max 30). Defaults to 10.
    """
    resp = _gh(f"/repos/{_ORG}/{repo}/commits?sha={branch}&per_page={min(limit, 30)}")
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    commits = resp.json()
    return {
        "repo": repo,
        "branch": branch,
        "commits": [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].splitlines()[0],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in commits
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
