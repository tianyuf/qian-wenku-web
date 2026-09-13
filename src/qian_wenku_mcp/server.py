"""MCP server exposing the Qian Xuesen corpus as agent tools.

Two modes:

* **stdio** (`qian-wenku-mcp`) — each researcher runs it locally with a
  `WENKU_BETA_PASSPHRASE` env var; the server does the beta login and
  returns results over stdio.

* **hosted HTTP** (`python -m qian_wenku_mcp.server`) — runs on the
  deployment host as a sibling service to the web app, exposes the MCP
  endpoint at /mcp with bearer-token auth, and is meant to be fronted by
  nginx.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

DEFAULT_BASE_URL = "https://wenku.qianxuesen.org"

# ---------------------------------------------------------------------------
# HTTP client with beta auth
# ---------------------------------------------------------------------------


class WenkuClient:
    """Synchronous HTTP client for the Qian Wenku JSON API.

    If a passphrase is configured and the server requires login,
    performs the one-time login POST and reuses the session
    cookie for subsequent requests.
    """

    def __init__(self, base_url: str, passphrase: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.passphrase = passphrase
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            follow_redirects=True,
        )
        self._authenticated = False

    def _login_if_needed(self) -> None:
        if self._authenticated:
            return
        if not self.passphrase:
            # No passphrase configured; hope the API is open.
            self._authenticated = True
            return
        resp = self._client.post(
            "/login",
            data={"passphrase": self.passphrase, "next": "/"},
        )
        # Login redirects on success; wrong passphrase re-renders the login page with an error.
        if resp.status_code == 200 and "Incorrect passphrase" in resp.text:
            raise PermissionError("Beta passphrase rejected by server")
        self._authenticated = True

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._login_if_needed()
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

_SERVER_INSTRUCTIONS = """\
Access to the Qian Xuesen (钱学森) corpus — nianpu (chronology), wenji
(collected works), and shuxin (letters). Use `search` to find entries by
text, `get_entry` for the full transcript of one entry, and `list_sources`
to see the available volumes.

## Citation rules (important)

Every entry returned by `search` and `get_entry` carries a stable
`permalink_url` (e.g. `https://wenku.qianxuesen.org/e/886987536f`) plus a
ready-made `citation` string. When you use information from the corpus in
your reply:

1. **Always cite the source entry.** Mention its date/title and include
   the permalink URL so the user can verify the original.
2. **Quote verbatim when quoting.** Copy the Chinese text exactly; do not
   paraphrase inside quotation marks.
3. **Distinguish corpus content from your own reasoning.** If you
   summarize, label clearly; do not silently merge multiple entries into
   one.
4. **When in doubt, follow up with `get_entry`.** Search results carry a
   truncated `content_preview`; the full text plus citation lives on the
   entry's canonical page.

Style suggestion for citations:

    「<direct quote>」 — 钱学森, *<source title>*, <date_display>
    <permalink_url>
"""

mcp = FastMCP(
    "qian-wenku",
    instructions=_SERVER_INSTRUCTIONS,
)

_base_url = os.environ.get("WENKU_BASE_URL", DEFAULT_BASE_URL)
_passphrase = (
    os.environ.get("WENKU_BETA_PASSPHRASE")
    or os.environ.get("WENKU_MCP_TOKEN", "")
)
_client = WenkuClient(_base_url, _passphrase)


def _permalink_url(permalink: str | None) -> str | None:
    """Absolute URL for an entry permalink, or None if missing."""
    if not permalink:
        return None
    return f"{_base_url}/e/{permalink}"


def _format_citation(entry: dict[str, Any]) -> str:
    """Build a compact, cite-ready string for one entry.

    Format:  钱学森,《我们要看到21世纪》,《钱学森文集（第6卷）》, 1989年1月. URL
    Falls back gracefully on missing fields.
    """
    parts: list[str] = []
    title = entry.get("title")
    source_title = (entry.get("source") or {}).get("title")
    date_display = entry.get("date_display") or entry.get("date_iso")

    if title:
        parts.append(f"《{title}》")
    if source_title:
        if title:
            parts.append(f"载《{source_title}》")
        else:
            parts.append(f"《{source_title}》")
    if date_display:
        parts.append(str(date_display))

    url = _permalink_url(entry.get("permalink"))
    body = ", ".join(parts) if parts else "钱学森文库条目"
    return f"{body}. {url}" if url else body + "."


def _enrich_entry(entry: dict[str, Any]) -> None:
    """Attach permalink_url + citation to an entry dict in-place."""
    entry["permalink_url"] = _permalink_url(entry.get("permalink"))
    entry["citation"] = _format_citation(entry)


def _enrich_search_results(payload: dict[str, Any]) -> dict[str, Any]:
    for entry in payload.get("results") or []:
        _enrich_entry(entry)
    return payload


def _enrich_entry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entry = payload.get("entry")
    if isinstance(entry, dict):
        _enrich_entry(entry)
    return payload


@mcp.prompt(name="citation-rules")
def citation_rules_prompt() -> str:
    """Instructions on how to cite entries from the Qian Wenku corpus.

    Attach this to your system prompt or pass it whenever the
    "qian-wenku" server is in play, so the agent always links back to
    sources with permalinks.
    """
    return _SERVER_INSTRUCTIONS


@mcp.tool
def search(
    q: str = "",
    person: str = "",
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    content_type: Optional[str] = None,
    source_id: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Full-text substring search across corpus entries.

    Searches the cleaned OCR text of all entries. Substring matching is
    intentional — works well for unsegmented Chinese.

    Args:
        q: Substring to find in entry content (Chinese or English).
        person: Filter to entries whose source title contains this name
            (e.g. "钱学森").
        year_start: Earliest year (inclusive).
        year_end: Latest year (inclusive).
        content_type: One of "nianpu", "wenji", or "shuxin".
        source_id: Restrict to a single source id (see list_sources()).
        limit: Max results (default 20, API cap 500).
        offset: Pagination offset.

    Returns:
        Dict with keys: results (list of entries), total (matching count),
        limit, offset, query, filters.
        Each result has: id, date_iso, date_display, date_precision,
        content_preview (300 chars), content_full, pages {start, end,
        pdf_start, pdf_end}, source {id, title, volume, content_type},
        permalink, permalink_url (absolute URL for citation),
        citation (formatted), plus title/source_attribution/footnotes
        for wenji entries.
    """
    params: dict[str, Any] = {"q": q, "limit": limit, "offset": offset}
    if person:
        params["person"] = person
    if year_start is not None:
        params["year_start"] = year_start
    if year_end is not None:
        params["year_end"] = year_end
    if content_type:
        params["content_type"] = content_type
    if source_id is not None:
        params["source_id"] = source_id
    return _enrich_search_results(_client.get("/api/search/", params=params))


@mcp.tool
def get_entry(entry_id: int) -> dict[str, Any]:
    """Fetch the full transcript and metadata of a single entry by id.

    Returns the entry's full content, plus prev/next/same-day navigation
    within its source so an agent can walk a sequence.

    Args:
        entry_id: Integer id of the entry (from search results).

    Returns:
        Dict with keys: entry (full content, date, pages, source,
        permalink_url, citation), navigation {prev, next, same_day}.
    """
    return _enrich_entry_payload(_client.get(f"/api/browse/entry/{entry_id}"))


@mcp.tool
def list_sources() -> dict[str, Any]:
    """List all sources (volumes) in the corpus with entry counts.

    Returns each source's id, title (e.g. "钱学森文集（第6卷）"), volume
    number, content type, entry count, and year range — everything needed
    to filter search by volume.

    Returns:
        Dict with keys: sources (list), each having id, title, volume,
        content_type, entry_count, year_range {min, max}.
    """
    stats = _client.get("/api/meta/stats")
    return {"sources": stats.get("sources", [])}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (per-user install)."""
    mcp.run()


def serve_http(host: str = "127.0.0.1", port: int = 8100) -> None:
    """Run the MCP server over HTTP behind the deployment's nginx.

    Expects the bearer-token value from ``WENKU_MCP_TOKEN`` (a shared
    secret — typically the same value as the web app's BETA_PASSPHRASE).
    Requests are rejected client-side by middleware before reaching the
    MCP stack, so any invalid token gets a plain 401.
    """
    import hmac as _hmac

    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    import uvicorn

    token = os.environ.get("WENKU_MCP_TOKEN", "")
    if not token:
        raise RuntimeError("WENKU_MCP_TOKEN is required for the hosted MCP server")

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            auth = request.headers.get("authorization", "")
            scheme, _, value = auth.partition(" ")
            if scheme.lower() != "bearer" or not _hmac.compare_digest(value, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    asgi_app = mcp.http_app(path="/mcp/rpc")
    app = Starlette(lifespan=asgi_app.lifespan)
    app.add_middleware(BearerAuthMiddleware)
    app.mount("/", asgi_app)

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true",
                        help="Serve over HTTP with bearer-token auth (hosted mode)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    if args.http:
        serve_http(host=args.host, port=args.port)
    else:
        main()
