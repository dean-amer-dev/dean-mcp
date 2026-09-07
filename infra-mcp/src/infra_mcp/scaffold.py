"""TOML spec generators for stateless (k3s) and stateful (Komodo) apps."""

from __future__ import annotations


def scaffold_stateless_spec(
    name: str,
    domain: str,
    port: int = 8000,
    has_database: bool = False,
) -> str:
    """Generate a stateless k3s app TOML spec."""
    db_secret_block = ""
    db_env_block = ""
    db_section = ""

    if has_database:
        db_env_block = f"""
[[components.env]]
name = "DB_PASSWORD"
secret_ref = {{ name = "postgres-credentials", key = "password" }}

[[components.env]]
name = "DATABASE_URL"
value = "postgres://{name}:$(DB_PASSWORD)@agent-kb.amer.dev:5432/{name}"
"""
        db_secret_block = f"""
[[secrets]]
bws_name = "{name}-postgres-password"
k8s_secret = "postgres-credentials"
k8s_key = "password"
generate = true
"""
        db_section = f"""
[database]
type = "postgres"
name = "{name}"
host = "agent-kb.amer.dev"
extensions = []
password_secret = "{name}-postgres-password"
"""

    return f"""[app]
name = "{name}"
domain = "{domain}"
namespace = "{name}"

[[components]]
name = "backend"
image = "amerenda/{name}:backend-latest"
port = {port}
replicas = 2
health_path = "/health"
ingress = true

[components.resources.requests]
cpu = "50m"
memory = "128Mi"

[components.resources.limits]
cpu = "200m"
memory = "256Mi"
{db_env_block}
{db_secret_block}
{db_section}
[uat]
enabled = true
replicas = 1

[uat.resources.requests]
cpu = "25m"
memory = "64Mi"

[uat.resources.limits]
cpu = "100m"
memory = "128Mi"

[cicd]
repo = "amerenda/{name}"
label = "deploy:{name}"
"""


def scaffold_stateful_stub(name: str, port: int = 8000) -> str:
    """Generate a Komodo docker compose file for a new stateful app's own directory."""
    return f"""name: {name}

services:
  {name}:
    image: amerenda/{name}:latest
    container_name: {name}
    restart: unless-stopped
    ports:
      - "{port}:{port}"
    environment:
      - EXAMPLE_SECRET=${{EXAMPLE_SECRET}}
    volumes:
      - {name}-data:/data

volumes:
  {name}-data:
"""


_SERVER_TAGS = {
    "mac-mini-m4": "mac-mini",
    "murderbot": "murderbot",
    "archlinux": "archlinux",
}


def scaffold_stateful_stack_block(name: str, description: str, server: str) -> str:
    """Generate the [[stack]] block for resource-sync/stacks.toml.

    webhook_force_deploy = true is always included, never optional -- see
    GITOPS_POLICY.md rule 5 (2026-09-07): every deploy=true stack must have
    this set or a push can fire the deploy webhook and still no-op instead
    of redeploying. Registering the webhook itself happens in a separate
    step (register_webhook) after this stack's PR is merged and Komodo has
    synced it, since the webhook URL needs Komodo's assigned stack UUID.
    """
    tag = _SERVER_TAGS.get(server, server)
    return f"""[[stack]]
name = "{name}"
description = "{description}"
tags = ["{tag}"]
deploy = true
[stack.config]
server = "{server}"
repo = "amerenda/komodo-dean-gitops"
branch = "main"
file_paths = ["{server}/{name}/compose.yaml"]
git_account = "amerenda"
project_name = "{name}"
webhook_force_deploy = true
"""
