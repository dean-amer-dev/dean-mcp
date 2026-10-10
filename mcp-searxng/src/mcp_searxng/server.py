"""mcp-searxng: Optimized web search and URL reading for small-context LLMs."""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Literal
from urllib.parse import urlparse

import html2text as _h2t
import httpx
import trafilatura
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

EMBED_URL = os.environ.get("EMBED_URL", "http://modernbert-embed-mini.llm.svc.cluster.local:8080/v1/embeddings")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomicai-modernbert-embed-base-8bit")
_CHUNK_WORDS = 220  # about 300 tokens

_KIND_ENGINES = {
    "web": "bing,yandex,wikipedia",
    "code": "github,stackoverflow,hackernews",
    "papers": "arxiv,google scholar,semantic scholar",
}
_PER_DOMAIN = 2

# In-memory search cache: (query, kind) -> (fetched_at, result)
_search_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_SEARCH_TTL = 600  # 10 minutes

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
        parsed = urlparse(url)
    except Exception:
        return url
    if parsed.netloc == "github.com":
        # one site hosts every repo, so cap per repo instead
        return "github.com/" + "/".join(parsed.path.strip("/").split("/")[:2])
    return parsed.netloc


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
    kind: Literal["web", "code", "papers"] = "web",
) -> dict:
    """Search the web and return scored results, at most 2 per domain.

    Use short keyword queries (e.g. 'llama.cpp speculative decoding'), not
    full sentences; sentences return junk.

    Args:
        query: Short keyword query.
        max_results: Max results to return (default 5, max 10).
        kind: 'web' searches Bing, Yandex and Wikipedia; 'code' searches
              GitHub, StackOverflow and Hacker News; 'papers' searches arXiv,
              Google Scholar and Semantic Scholar.
    """
    max_results = min(max_results, 10)
    if kind not in _KIND_ENGINES:
        return {"error": f"kind must be one of {sorted(_KIND_ENGINES)}"}
    cache_key = (query, kind)
    entry = _search_cache.get(cache_key)
    if entry and (time.monotonic() - entry[0]) < _SEARCH_TTL:
        return _format(query, entry[1][:max_results])
    params: dict[str, str | int] = {"q": query, "format": "json", "engines": _KIND_ENGINES[kind]}

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

    # Keep the best _PER_DOMAIN results per domain, ranked by score
    ranked_all = sorted(raw, key=lambda r: r.get("score") or 0, reverse=True)
    per_domain: dict[str, int] = {}
    ranked: list[dict] = []
    for r in ranked_all:
        domain = _get_domain(r.get("url", ""))
        if per_domain.get(domain, 0) >= _PER_DOMAIN:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        ranked.append(r)
        if len(ranked) >= 10:
            break

    if len(_search_cache) >= _CACHE_MAX:
        del _search_cache[min(_search_cache, key=lambda k: _search_cache[k][0])]
    _search_cache[cache_key] = (time.monotonic(), ranked)
    return _format(query, ranked[:max_results])


def _format(query: str, ranked: list[dict]) -> dict:
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
    extracted = trafilatura.extract(html, output_format="markdown")
    if extracted:
        return extracted
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


def _chunks(text: str) -> list[str]:
    words = text.split()
    return [" ".join(words[i : i + _CHUNK_WORDS]) for i in range(0, len(words), _CHUNK_WORDS)]


def _fetch_text(url: str) -> str:
    try:
        resp = httpx.get(
            url,
            timeout=5,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; mcp-searxng/1.0)"},
        )
        resp.raise_for_status()
        return _to_markdown(resp.text)
    except Exception:
        return ""


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@mcp.tool()
def search_deep(query: str, kind: Literal["web", "code", "papers"] = "web") -> dict:
    """Search, fetch the top 10 pages, and return the 5 passages most relevant to the query.

    Slower than search (fetches pages) but returns answer-bearing passages
    instead of snippets. Use short keyword queries. If the embedding service
    is down, falls back to search order.

    Args:
        query: Short keyword query.
        kind: 'web', 'code' or 'papers', as for search.
    """
    found = search(query, max_results=10, kind=kind)
    if "error" in found or not found.get("results"):
        return found
    urls = [r["url"] for r in found["results"] if r.get("url")]
    with ThreadPoolExecutor(max_workers=10) as pool:
        texts = list(pool.map(_fetch_text, urls))

    items: list[tuple[str, str]] = []  # (url, chunk)
    for url, text in zip(urls, texts):
        for chunk in _chunks(text):
            items.append((url, chunk))
    if not items:
        return {**found, "note": "no page content could be fetched; returning search order"}

    try:
        resp = httpx.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "input": [query] + [c for _, c in items]},
            timeout=60,
        )
        resp.raise_for_status()
        vectors = [d["embedding"] for d in sorted(resp.json()["data"], key=lambda d: d["index"])]
        qv, cvs = vectors[0], vectors[1:]
        scored = sorted(
            ((_cosine(qv, v), url, chunk) for (url, chunk), v in zip(items, cvs)),
            key=lambda t: t[0],
            reverse=True,
        )[:5]
    except Exception as e:
        return {**found, "note": f"embedding failed ({e}); returning search order"}

    passages = [{"url": u, "score": round(sc, 4), "text": c} for sc, u, c in scored]
    formatted = "\n\n".join(f"[{i}] {p['url']}\n{p['text']}" for i, p in enumerate(passages, 1))
    return {"query": query, "count": len(passages), "passages": passages, "formatted": formatted}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, show_banner=False)
