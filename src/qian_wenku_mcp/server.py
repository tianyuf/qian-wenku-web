"""MCP server exposing the Qian Xuesen corpus as agent tools.

Runs over stdio; each researcher runs it locally with their own
passphrase. The server is a thin JSON client of the public web
app's API — auth is whatever the web app enforces (beta passphrase
cookie session).
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

mcp = FastMCP(
    "qian-wenku",
    instructions=(
        "Access to the Qian Xuesen (钱学森) corpus — nianpu (chronology), "
        "wenji (collected works), and shuxin (letters). Use search to find "
        "entries by text, use get_entry for the full transcript of one entry, "
        "and list_sources to see the available volumes. For access, contact "
        "mail@qianxuesen.org."
    ),
)

_base_url = os.environ.get("WENKU_BASE_URL", DEFAULT_BASE_URL)
_passphrase = os.environ.get("WENKU_BETA_PASSPHRASE", "")
_client = WenkuClient(_base_url, _passphrase)


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
        plus title/source_attribution/footnotes for wenji entries.
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
    return _client.get("/api/search/", params=params)


@mcp.tool
def get_entry(entry_id: int) -> dict[str, Any]:
    """Fetch the full transcript and metadata of a single entry by id.

    Returns the entry's full content, plus prev/next/same-day navigation
    within its source so an agent can walk a sequence.

    Args:
        entry_id: Integer id of the entry (from search results).

    Returns:
        Dict with keys: entry (full content, date, pages, source),
        navigation {prev, next, same_day}.
    """
    return _client.get(f"/api/browse/entry/{entry_id}")


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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
