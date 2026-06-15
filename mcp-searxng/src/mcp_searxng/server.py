"""mcp-searxng: Web search and URL reading via SearXNG."""

from __future__ import annotations

import os

import html2text as _h2t
import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng.searxng.svc.cluster.local:8080")

mcp = FastMCP(
    "mcp-searxng",
    instructions="Web search and URL reading via SearXNG. Use searxng_web_search for queries, web_url_read to fetch page content.",
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool()
def searxng_web_search(query: str, max_results: int = 5) -> dict:
    """Search the web using the local SearXNG instance.

    Returns titles, URLs, and content snippets for each result.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return (default 5, max 10).
    """
    max_results = min(max_results, 10)
    try:
        resp = httpx.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json"},
            timeout=15,
            headers={"User-Agent": "mcp-searxng/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    results = data.get("results", [])[:max_results]
    if not results:
        return {"results": [], "message": f"No results found for: {query}"}

    formatted = "\n\n".join(
        f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nSnippet: {r.get('content', '')[:300]}"
        for r in results
    )
    return {"count": len(results), "results": results, "formatted": formatted}


@mcp.tool()
def web_url_read(url: str, max_chars: int = 8000) -> dict:
    """Fetch a URL and return its content as markdown.

    Converts HTML to readable markdown. Useful for reading docs, release notes,
    GitHub issues, or any web page.

    Args:
        url: Full URL to fetch.
        max_chars: Max characters of markdown to return (default 8000).
    """
    try:
        resp = httpx.get(
            url,
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; mcp-searxng/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return {"error": str(e)}

    converter = _h2t.HTML2Text()
    converter.ignore_images = True
    converter.ignore_links = False
    converter.body_width = 0
    markdown = converter.handle(html)

    truncated = len(markdown) > max_chars
    if truncated:
        markdown = markdown[:max_chars] + f"\n\n…[truncated at {max_chars} chars, full length: {len(markdown)}]"

    return {"url": url, "content": markdown, "truncated": truncated}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
