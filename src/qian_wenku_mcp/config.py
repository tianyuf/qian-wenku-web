"""CLI installer: adds the qian-wenku MCP server to OpenCode's user config.

Usage:
    qian-wenku-mcp-config YOUR_TOKEN

The token is the shared beta passphrase for https://wenku.qianxuesen.org.
Writes ``mcp.qian-wenku`` into ``~/.config/opencode/opencode.json``,
preserving any existing top-level fields; refuses to overwrite an existing
``mcp.qian-wenku`` entry unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER_URL = "https://wenku.qianxuesen.org/mcp/rpc"
CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"
SERVER_KEY = "qian-wenku"


def build_entry(token: str) -> dict:
    return {
        "type": "remote",
        "url": SERVER_URL,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("token", help="Access token for the Qian Wenku MCP server")
    p.add_argument("--force", action="store_true",
                   help=f"Overwrite an existing mcp.{SERVER_KEY} entry")
    args = p.parse_args(argv)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError as e:
            print(f"error: {CONFIG_PATH} is not valid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(cfg, dict):
            print(f"error: {CONFIG_PATH} top level is not an object", file=sys.stderr)
            return 2

    mcp = cfg.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        print(f"error: {CONFIG_PATH} \"mcp\" key is not an object", file=sys.stderr)
        return 2

    if SERVER_KEY in mcp and not args.force:
        print(f"error: mcp.{SERVER_KEY} already exists; pass --force to overwrite",
              file=sys.stderr)
        return 1

    mcp[SERVER_KEY] = build_entry(args.token)

    # Preserve $schema if present
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {CONFIG_PATH}")
    print(f"  mcp.{SERVER_KEY}.url = {SERVER_URL}")
    print(f"  mcp.{SERVER_KEY}.type = remote")
    print(f"  headers.Authorization = Bearer <token>")
    print()
    print("Restart OpenCode for the change to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
