# github-mcp

Read-only GitHub repo browsing MCP server for the `amerenda` org. Exposed to LiteLLM as an MCP tool server via cluster-internal DNS.

## Tools

| Tool | Description |
|------|-------------|
| `list_files` | List files/dirs at a path in a repo |
| `read_file` | Read a file (capped at 32KB) |
| `search_code` | GitHub code search within a repo |
| `list_prs` | List pull requests (open/closed/all) |
| `get_pr_diff` | Fetch unified diff for a PR (capped at 32KB) |
| `list_commits` | List recent commits on a branch |
| `get_repo_tree` | Get the full directory tree up to N levels deep — use this to explore or "clone" a repo |

All tools operate on the `amerenda` org by default (overridable via `GITHUB_ORG` env var).

## Authentication — amerenda-coder GitHub App

This server authenticates to the GitHub API using the **amerenda-coder GitHub App** (not a personal access token, not a static bearer token on the MCP endpoint).

### How it works

1. The server holds three secrets from Bitwarden (injected via k3s ExternalSecret):
   - `GITHUB_APP_ID` — the numeric App ID for amerenda-coder
   - `GITHUB_APP_PRIVATE_KEY` — the RSA private key for the App
   - `GITHUB_APP_INSTALLATION_ID` — the installation ID for the `amerenda` org

2. On startup and on each GitHub API call, the server:
   - Signs a short-lived JWT (10-minute expiry) with the private key using RS256
   - POSTs that JWT to `https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens`
   - Receives a GitHub installation access token (1-hour expiry)
   - Caches that token and reuses it until 60 seconds before expiry

3. All GitHub API requests use `Authorization: token <installation_token>`.

### What this means for the MCP endpoint

**The `/mcp` endpoint has no bearer token protection.** There is no `GITHUB_MCP_TOKEN` or any equivalent. This is intentional:

- The server is deployed inside the k3s cluster and accessed by LiteLLM over cluster-internal DNS (`github-mcp-server.github-mcp.svc.cluster.local:8000`)
- The ingress at `github-mcp.amer.dev` is for admin/debug access within the trusted network
- Security is provided by GitHub App credentials, not by protecting the MCP transport layer

**Do not add a bearer token to the MCP endpoint** — it is not part of this design and will break the LiteLLM integration.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | Yes | amerenda-coder GitHub App numeric ID |
| `GITHUB_APP_PRIVATE_KEY` | Yes | RSA private key (PEM, `\n` escaped to literal `\\n` in BWS) |
| `GITHUB_APP_INSTALLATION_ID` | Yes | Installation ID for the `amerenda` org |
| `GITHUB_ORG` | No | GitHub org to scope all queries to (default: `amerenda`) |
| `MAX_FILE_CHARS` | No | Max characters returned by `read_file` (default: `32768`) |

## Deployment

Runs as a k3s Deployment in the `github-mcp` namespace. Secrets are injected via ExternalSecret from Bitwarden:

```
BWS key: github-amerenda-coder-app-id         → GITHUB_APP_ID
BWS key: github-amerenda-coder-private-key    → GITHUB_APP_PRIVATE_KEY
BWS key: github-amerenda-coder-installation-id → GITHUB_APP_INSTALLATION_ID
```

LiteLLM connects to it at:
```
http://github-mcp-server.github-mcp.svc.cluster.local:8000/mcp
```

## Local Development

```bash
export GITHUB_APP_ID=<id>
export GITHUB_APP_PRIVATE_KEY="$(cat amerenda-coder.pem)"
export GITHUB_APP_INSTALLATION_ID=<installation-id>

pip install -e .
python -m github_mcp.server
# MCP endpoint: http://localhost:8000/mcp
# Health:       http://localhost:8000/health
```
