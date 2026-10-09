FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir . && useradd --system --uid 10001 mcp

USER 10001
EXPOSE 8000
CMD ["model-switch-mcp"]
