"""Tests for the MCP server module.

Covers only local wiring — tool registration, auth-handling shape.
End-to-end behavior against a live server is exercised manually.
"""

import pytest

from fastmcp import Client


@pytest.mark.asyncio
async def test_tools_registered():
    from qian_wenku_mcp.server import mcp

    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["get_entry", "list_sources", "search"]


def test_base_url_and_passphrase_env(monkeypatch):
    import importlib
    import qian_wenku_mcp.server as srv

    monkeypatch.setenv("WENKU_BASE_URL", "http://example.invalid:9999/")
    monkeypatch.setenv("WENKU_BETA_PASSPHRASE", "secret")
    importlib.reload(srv)
    assert srv._client.base_url == "http://example.invalid:9999"
    assert srv._client.passphrase == "secret"
    monkeypatch.delenv("WENKU_BASE_URL")
    monkeypatch.delenv("WENKU_BETA_PASSPHRASE")
    importlib.reload(srv)


def test_base_url_trailing_slash_normalised():
    from qian_wenku_mcp.server import WenkuClient

    c = WenkuClient("https://example.com/", "")
    assert c.base_url == "https://example.com"
