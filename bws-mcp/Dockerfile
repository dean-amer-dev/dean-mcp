FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# BWS CLI
RUN ARCH=$(uname -m); \
    case "$ARCH" in \
      x86_64)  TRIPLE="x86_64-unknown-linux-gnu" ;; \
      aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/bitwarden/sdk-sm/releases/latest/download/bws-${TRIPLE}.tar.gz" \
    | tar -xz -C /usr/local/bin bws

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY src/ src/

ENV BWS_TIER=read
EXPOSE 8000
CMD ["python", "-m", "bws_mcp.server"]
