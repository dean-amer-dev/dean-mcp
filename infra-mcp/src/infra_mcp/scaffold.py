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
    """Generate a Komodo docker compose service block stub for a stateful app."""
    return f"""  # Add to komodo-dean-gitops/mac-mini-m4/<stack>/compose.yaml under 'services:'
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

  # Add under top-level 'volumes:' key:
  # {name}-data:
"""
