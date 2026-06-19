"""mcp-searxng: Optimized web search and URL reading for small-context LLMs."""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import html2text as _h2t
import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng.searxng.svc.cluster.local:8080")
_MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "5"))
_MAX_SNIPPET = int(os.environ.get("MAX_CONTENT_CHARS", "200"))
_MAX_URL_CHARS = int(os.environ.get("MAX_URL_CONTENT_CHARS", "4000"))

# In-memory URL cache: url -> (fetched_at, markdown)
_url_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX = 50

mcp = FastMCP(
    "mcp-searxng",
    instructions=(
        "Optimized web search and URL reading for small-context models. "
        "search returns scored, deduplicated results — fewer, better hits. "
        "read_url supports read_headings=True (page outline only) and section= (extract one section). "
        "Use read_headings first on long docs to locate the right section before fetching full content."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return url


def _cache_get(url: str) -> str | None:
    entry = _url_cache.get(url)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(url: str, markdown: str) -> None:
    if len(_url_cache) >= _CACHE_MAX:
        oldest = min(_url_cache, key=lambda k: _url_cache[k][0])
        del _url_cache[oldest]
    _url_cache[url] = (time.monotonic(), markdown)


@mcp.tool()
def search(
    query: str,
    max_results: int = _MAX_RESULTS,
    engines: str | None = None,
) -> dict:
    """Search the web and return scored, deduplicated results.

    Results are ranked by relevance score and deduplicated by domain — you won't
    get three StackOverflow links eating your context. Only the top result per
    domain is kept.

    For code/library questions, pass engines='stackoverflow,github' to skip
    general web noise. Omit engines to use the instance defaults (Google +
    GitHub + StackOverflow).

    Args:
        query: Search query.
        max_results: Max results to return (default 5, max 10).
        engines: Comma-separated engine names, e.g. 'google' or 'stackoverflow,github'.
    """
    max_results = min(max_results, 10)
    params: dict[str, str | int] = {"q": query, "format": "json"}
    if engines:
        params["engines"] = engines

    try:
        resp = httpx.get(
            f"{SEARXNG_URL}/search",
            params=params,
            timeout=15,
            headers={"User-Agent": "mcp-searxng/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    raw = data.get("results", [])
    if not raw:
        return {"count": 0, "query": query, "formatted": f"No results for: {query}"}

    # Deduplicate by domain, keeping the highest-scoring result per domain
    by_domain: dict[str, dict] = {}
    for r in raw:
        domain = _get_domain(r.get("url", ""))
        score = r.get("score") or 0
        if domain not in by_domain or score > (by_domain[domain].get("score") or 0):
            by_domain[domain] = r

    # Sort by score descending, take top N
    ranked = sorted(by_domain.values(), key=lambda r: r.get("score") or 0, reverse=True)[:max_results]

    lines = []
    for i, r in enumerate(ranked, 1):
        score = r.get("score") or 0
        title = (r.get("title") or "").strip()
        url = r.get("url") or ""
        snippet = (r.get("content") or "").strip()[:_MAX_SNIPPET]
        lines.append(f"[{i}] {title}\n    {url}\n    score={score:.3f}  {snippet}")

    return {
        "count": len(ranked),
        "query": query,
        "results": ranked,
        "formatted": "\n\n".join(lines),
    }


def _to_markdown(html: str) -> str:
    converter = _h2t.HTML2Text()
    converter.ignore_images = True
    converter.ignore_links = False
    converter.body_width = 0
    return converter.handle(html)


def _extract_section(markdown: str, keyword: str) -> str:
    """Return the content under the first heading containing keyword (case-insensitive)."""
    lines = markdown.splitlines()
    kw = keyword.lower()
    start = None
    level = 0
    for i, line in enumerate(lines):
        if line.startswith("#") and kw in line.lower():
            start = i
            level = len(line) - len(line.lstrip("#"))
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#"):
            curr = len(lines[i]) - len(lines[i].lstrip("#"))
            if curr <= level:
                end = i
                break
    return "\n".join(lines[start:end])


@mcp.tool()
def read_url(
    url: str,
    max_chars: int = _MAX_URL_CHARS,
    read_headings: bool = False,
    section: str | None = None,
) -> dict:
    """Fetch a URL and return its content as markdown.

    Strategy for long pages:
    1. Call with read_headings=True to get the page outline cheaply.
    2. Call again with section='Heading Text' to extract just that section.
    3. Only read the full page if you need it end-to-end.

    Fetched pages are cached for 5 minutes — repeated calls to the same URL
    are free.

    Args:
        url: Full URL to fetch.
        max_chars: Max characters to return (default 4000). Ignored when
                   read_headings=True or section is set and the section fits.
        read_headings: Return only the heading outline (H1–H6). Fast and cheap.
        section: Heading keyword (case-insensitive substring). Returns just
                 that section. If not found, returns an error with heading list.
    """
    markdown = _cache_get(url)
    if markdown is None:
        try:
            resp = httpx.get(
                url,
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; mcp-searxng/1.0)"},
            )
            resp.raise_for_status()
            markdown = _to_markdown(resp.text)
        except Exception as e:
            return {"url": url, "error": str(e)}
        _cache_set(url, markdown)

    if read_headings:
        headings = [l for l in markdown.splitlines() if l.startswith("#")]
        return {"url": url, "headings": headings, "content": "\n".join(headings)}

    if section:
        content = _extract_section(markdown, section)
        if not content:
            headings = [l for l in markdown.splitlines() if l.startswith("#")]
            return {
                "url": url,
                "error": f"Section '{section}' not found.",
                "available_headings": headings,
            }
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n…[truncated at {max_chars} chars]"
        return {"url": url, "section": section, "content": content}

    truncated = len(markdown) > max_chars
    content = markdown[:max_chars]
    if truncated:
        content += f"\n\n…[truncated at {max_chars} chars, full length: {len(markdown)}. Use read_headings=True to navigate.]"
    return {"url": url, "content": content, "truncated": truncated}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
