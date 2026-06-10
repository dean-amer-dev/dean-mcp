FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

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
COPY src/ src/
RUN pip install --no-cache-dir -e .

ENV BWS_TIER=read
EXPOSE 8000
CMD ["python", "-m", "bws_mcp.server"]
