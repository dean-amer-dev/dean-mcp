FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    make \
    unzip \
    gpg \
    gpg-agent \
    && rm -rf /var/lib/apt/lists/*

# OpenTofu
RUN curl -fsSL https://get.opentofu.org/install-opentofu.sh | sh -s -- --install-method standalone

# BWS CLI
ARG BWS_VERSION=2.1.0
RUN ARCH=$(uname -m); \
    case "$ARCH" in \
      x86_64)  TRIPLE="x86_64-unknown-linux-gnu" ;; \
      aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/bitwarden/sdk-sm/releases/download/bws-v${BWS_VERSION}/bws-${TRIPLE}-${BWS_VERSION}.zip" \
    -o /tmp/bws.zip && \
    unzip -q /tmp/bws.zip -d /usr/local/bin bws && \
    chmod +x /usr/local/bin/bws && \
    rm /tmp/bws.zip

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY src/ src/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV APP_FACTORY_DIR=/repos/app-factory
ENV GITOPS_DIR=/repos/k3s-dean-gitops
ENV KOMODO_DIR=/repos/komodo-dean-gitops

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
