"""MCP server exposing the Qian Xuesen corpus as agent tools.

Two modes:

* **stdio** (`qian-wenku-mcp`) — each researcher runs it locally with the
  configured service credential in `WENKU_SERVICE_TOKEN`; the server calls the web API
  and returns results over stdio.

* **hosted HTTP** (`python -m qian_wenku_mcp.server`) — runs on the
  deployment host as a sibling service to the web app, accepts active
  MCP tokens as bearer tokens, and is meant to be fronted by nginx.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

from qian_wenku_web.access import resolve_mcp_token, verify_bearer_token

DEFAULT_BASE_URL = "https://wenku.qianxuesen.org"

# Bearer token of the caller, set by the hosted middleware per request.
_caller_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "caller_token", default=""
)

# ---------------------------------------------------------------------------
# HTTP client with beta auth
# ---------------------------------------------------------------------------


class WenkuClient:
    """Synchronous HTTP client for the Qian Wenku JSON API.

    Authenticates with the shared service token via the X-Service-Token
    header on every request.
    """

    def __init__(self, base_url: str, service_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            follow_redirects=True,
            headers=(
                {"X-Service-Token": service_token} if service_token else {}
            ),
        )

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        user_authorization: str = "",
    ) -> dict[str, Any]:
        headers = (
            {"X-User-Authorization": user_authorization} if user_authorization else {}
        )
        resp = self._client.get(path, params=params, headers=headers)
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
text, `get_entry` for the full transcript of one entry, `list_sources`
to see the available volumes, `list_favorites` for the user's
favorited entries, and `list_notes` for the user's annotations on
entries (both require an individual MCP token).

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
_service_token = os.environ.get("WENKU_SERVICE_TOKEN", "")
_client = WenkuClient(_base_url, _service_token)


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


@mcp.tool
def list_favorites() -> dict[str, Any]:
    """List the current user's favorited entries.

    Only available on the hosted server when connected with an individual
    MCP token. Returns each favorite's entry id, title, source, date, page,
    permalink_url and citation, ordered newest first. Use get_entry on any
    of the returned entry ids for the full transcript.
    """
    if not _service_token:
        return {
            "error": "favorites are only available on the hosted server "
            "with an individual MCP token"
        }
    token = _caller_token.get()
    if not token:
        return {"error": "no user token; connect with your personal MCP token"}
    payload = _client.get(
        "/api/favorites",
        user_authorization=f"Bearer {token}",
    )
    favorites = payload.get("favorites", [])
    for favorite in favorites:
        favorite["permalink_url"] = _permalink_url(favorite.get("permalink"))
        favorite.pop("permalink", None)
        favorite["citation"] = (
            f"{favorite.get('source_title') or ''}，"
            f"{favorite.get('title') or ''}，{favorite.get('date_display') or ''}，"
            f"第{favorite.get('start_page')}页。"
        )
    return {"favorites": favorites, "total": payload.get("total", 0)}


@mcp.tool
def list_notes(entry_id: Optional[int] = None) -> dict[str, Any]:
    """List the current user's annotations (comments on entries).

    Only available on the hosted server when connected with an individual
    MCP token. Without entry_id returns all notes across entries, newest
    first. Each note has: entry_id, quote (the highlighted text, if any),
    body, and timestamps. Use get_entry on entry_id for the full context.
    """
    if not _service_token:
        return {
            "error": "notes are only available on the hosted server "
            "with an individual MCP token"
        }
    token = _caller_token.get()
    if not token:
        return {"error": "no user token; connect with your personal MCP token"}
    params: dict[str, Any] = {}
    if entry_id is not None:
        params["entry_id"] = entry_id
    return _client.get("/api/notes", params=params, user_authorization=f"Bearer {token}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (per-user install)."""
    mcp.run()


def serve_http(host: str = "127.0.0.1", port: int = 8100) -> None:
    """Run the MCP server over HTTP behind the deployment's nginx.

    Accepts active individual MCP tokens plus an optional operator token.
    Requests are rejected by middleware before reaching the MCP stack, so
    any invalid token gets a plain 401.
    """
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    import uvicorn

    operator_token = os.environ.get("WENKU_MCP_TOKEN", "")
    access_db_path = os.environ.get("ACCESS_DATABASE_PATH", "")
    access_code_secret = os.environ.get("ACCESS_CODE_SECRET", "")
    if access_db_path and not access_code_secret:
        raise RuntimeError("ACCESS_CODE_SECRET is required for individual MCP tokens")
    if not operator_token and not access_db_path:
        raise RuntimeError("Hosted MCP authentication is not configured")
    if not _service_token:
        raise RuntimeError(
            "WENKU_SERVICE_TOKEN is required for hosted MCP web API access"
        )

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            auth = request.headers.get("authorization", "")
            scheme, separator, value = auth.partition(" ")
            is_individual = (
                scheme.lower() == "bearer"
                and separator
                and access_db_path
                and await asyncio.to_thread(
                    resolve_mcp_token,
                    access_db_path,
                    access_code_secret,
                    value,
                )
                is not None
            )
            valid = is_individual or await asyncio.to_thread(
                verify_bearer_token,
                auth,
                operator_token,
                access_db_path,
                access_code_secret,
            )
            if not valid:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if is_individual:
                _caller_token.set(value)
            else:
                _caller_token.set("")
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
