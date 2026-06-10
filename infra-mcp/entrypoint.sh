#!/bin/bash
set -e

# Clone repos if not already present (k3s deployment path)
# For local stdio, APP_FACTORY_DIR and GITOPS_DIR point to existing clones.
clone_if_missing() {
  local dir="$1"
  local repo="$2"
  if [ ! -d "$dir/.git" ]; then
    echo "Cloning $repo into $dir..."
    mkdir -p "$(dirname "$dir")"
    git clone "https://x-access-token:${CODER_APP_PRIVATE_KEY_TOKEN}@github.com/amerenda/${repo}.git" "$dir"
  fi
}

if [ "${CLONE_REPOS:-false}" = "true" ]; then
  clone_if_missing "$APP_FACTORY_DIR"   "app-factory"
  clone_if_missing "$GITOPS_DIR"        "k3s-dean-gitops"
  clone_if_missing "$KOMODO_DIR"        "komodo-dean-gitops"
fi

exec python -m infra_mcp.server "$@"
