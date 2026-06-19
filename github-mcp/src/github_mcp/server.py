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
_DEFAULT_ORG = os.environ.get("GITHUB_ORG", "amerenda")
_MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", "32768"))

mcp = FastMCP(
    "github-mcp",
    instructions=(
        "Read-only GitHub repo browsing. "
        "The repo parameter accepts 'name' (defaults to the amerenda org) or 'owner/name' "
        "for any public repo (e.g. 'openai/openai-python'). "
        "Use tree to explore a repo layout, read to read a specific file "
        "(capped at 32KB), search for symbol/string searches, prs for open PRs, "
        "pr_diff to review a PR, and commits for recent history."
    ),
)


_repo_cache: dict[str, str] = {}
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
        follow_redirects=True,
    )


def _resolve_repo(repo: str) -> str:
    """Resolve a bare repo name to 'owner/name'.

    1. 'owner/name' → returned as-is.
    2. 'name' → tries amerenda/name first.
    3. If not found, searches GitHub repositories by name and picks the best
       match (exact name match preferred, then highest stars).
    Results are cached in-process for the lifetime of the server.
    """
    if "/" in repo:
        return repo
    if repo in _repo_cache:
        return _repo_cache[repo]

    # 1. Try default org
    if _gh(f"/repos/{_DEFAULT_ORG}/{repo}").is_success:
        _repo_cache[repo] = f"{_DEFAULT_ORG}/{repo}"
        return _repo_cache[repo]

    # 2. Search GitHub for the most likely repo
    r = _gh(f"/search/repositories?q={repo}+in:name&per_page=5&sort=stars&order=desc")
    if r.is_success:
        items = r.json().get("items", [])
        if items:
            exact = [i for i in items if i["name"].lower() == repo.lower()]
            best = (exact[0] if exact else items[0])["full_name"]
            _repo_cache[repo] = best
            return best

    # 3. Fall back — will produce a clear 404 error in the calling tool
    return f"{_DEFAULT_ORG}/{repo}"


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool()
def ls(repo: str, path: str = "", ref: str = "main") -> dict:
    """List files and directories at a path in a GitHub repo.

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo, e.g. 'torvalds/linux'.
        path: Directory path inside the repo, e.g. 'agents/coder'. Empty string = repo root.
        ref: Git ref (branch, tag, or SHA). Defaults to 'main'.
    """
    full_repo = _resolve_repo(repo)
    url = f"/repos/{full_repo}/contents/{path}"
    if ref != "main":
        url += f"?ref={ref}"
    resp = _gh(url)
    if not resp.is_success:
        return {"error": f"{resp.status_code} — repo '{full_repo}' not found or not accessible. Try 'owner/repo' format, e.g. 'open-webui/open-webui'."}
    items = resp.json()
    if isinstance(items, list):
        return {
            "repo": repo,
            "path": path,
            "entries": [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items],
        }
    return {"repo": repo, "path": path, "type": "file", "size": items.get("size")}


@mcp.tool()
def read(repo: str, path: str, ref: str = "main") -> dict:
    """Read a file from a GitHub repo (response capped at 32KB).

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo, e.g. 'torvalds/linux'.
        path: File path inside the repo, e.g. 'agents/coder/agent.py'.
        ref: Git ref. Defaults to 'main'.
    """
    full_repo = _resolve_repo(repo)
    url = f"/repos/{full_repo}/contents/{path}"
    if ref != "main":
        url += f"?ref={ref}"
    resp = _gh(url)
    if not resp.is_success:
        return {"error": f"{resp.status_code} — '{full_repo}/{path}' not found. If the repo is outside the amerenda org, use 'owner/repo' format."}
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
def search(repo: str, query: str) -> dict:
    """Search for code in a GitHub repo using GitHub code search.

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo.
        query: Search query, e.g. 'def build_agent' or 'hatchet.task'.
    """
    full_repo = _resolve_repo(repo)
    resp = _gh(f"/search/code?q={query}+repo:{full_repo}&per_page=10")
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    items = resp.json().get("items", [])
    return {
        "count": len(items),
        "results": [{"path": i["path"], "url": i["html_url"]} for i in items],
    }


@mcp.tool()
def prs(repo: str, state: str = "open") -> dict:
    """List pull requests in a GitHub repo.

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo.
        state: 'open', 'closed', or 'all'. Defaults to 'open'.
    """
    full_repo = _resolve_repo(repo)
    resp = _gh(f"/repos/{full_repo}/pulls?state={state}&per_page=20")
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
def pr_diff(repo: str, pr_number: int) -> dict:
    """Fetch the unified diff for a pull request (capped at 32KB).

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo.
        pr_number: Pull request number.
    """
    full_repo = _resolve_repo(repo)
    resp = _gh(
        f"/repos/{full_repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    if not resp.is_success:
        return {"error": resp.text, "status": resp.status_code}
    diff = resp.text
    truncated = len(diff) > _MAX_FILE_CHARS
    return {"repo": repo, "pr": pr_number, "diff": diff[:_MAX_FILE_CHARS], "truncated": truncated}


@mcp.tool()
def commits(repo: str, branch: str = "main", limit: int = 10) -> dict:
    """List recent commits on a branch.

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo.
        branch: Branch name. Defaults to 'main'.
        limit: Number of commits to return (max 30). Defaults to 10.
    """
    full_repo = _resolve_repo(repo)
    resp = _gh(f"/repos/{full_repo}/commits?sha={branch}&per_page={min(limit, 30)}")
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


@mcp.tool()
def tree(repo: str, path: str = "", ref: str = "main", depth: int = 2) -> dict:
    """Get the full directory tree of a repo up to the specified depth.

    Use this to explore or 'clone' a repo — returns the complete file/directory
    layout in one call instead of repeated list_files calls.

    Args:
        repo: 'name' (amerenda org) or 'owner/name' for any public repo, e.g. 'open-webui/open-webui'.
        path: Root path to start from. Empty string = repo root.
        ref: Git ref (branch, tag, or SHA). Defaults to 'main'.
        depth: Directory levels to recurse (1–4). Defaults to 2.
    """
    depth = max(1, min(depth, 4))
    full_repo = _resolve_repo(repo)

    def _walk(p: str, remaining: int) -> list[dict] | dict:
        url = f"/repos/{full_repo}/contents/{p}"
        if ref != "main":
            url += f"?ref={ref}"
        resp = _gh(url)
        if not resp.is_success:
            if p == path:
                # Root-level failure — return an informative error so the model
                # can retry with 'owner/repo' format instead of silently getting an empty tree.
                return [{"error": f"{resp.status_code} — repo '{full_repo}' not found or not accessible. Try 'owner/repo' format, e.g. 'open-webui/open-webui'."}]
            return []
        items = resp.json()
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            entry = {"name": item["name"], "type": item["type"], "path": item["path"]}
            if item["type"] == "dir" and remaining > 1:
                entry["children"] = _walk(item["path"], remaining - 1)
            result.append(entry)
        return result

    return {"repo": repo, "path": path or "/", "ref": ref, "depth": depth, "tree": _walk(path, depth)}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
