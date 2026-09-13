"""CLI installer: adds the qian-wenku MCP server to OpenCode's user config.

Usage:
    qian-wenku-mcp-config YOUR_TOKEN
    qian-wenku-mcp-config YOUR_TOKEN --force   # overwrite existing entry

Prefers ``~/.config/opencode/opencode.jsonc`` when it exists (the default
layout for many OpenCode installs); otherwise uses ``opencode.json``. If
neither exists, creates ``opencode.json``.

Merges the ``qian-wenku`` entry into ``mcp`` without touching other keys
or comments. Idempotent: running twice with the same token is a no-op;
running with a different token refuses unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SERVER_URL = "https://wenku.qianxuesen.org/mcp/rpc"
CONFIG_DIR = Path.home() / ".config" / "opencode"
PREFERRED = CONFIG_DIR / "opencode.jsonc"
FALLBACK = CONFIG_DIR / "opencode.json"
SERVER_KEY = "qian-wenku"


def build_entry(token: str) -> dict:
    return {
        "type": "remote",
        "url": SERVER_URL,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def strip_jsonc_comments(text: str) -> str:
    """Remove // line and /* block */ comments without touching string values."""
    def strip(s: str) -> str:
        out = []
        i = 0
        in_str = False
        while i < len(s):
            c = s[i]
            if in_str:
                out.append(c)
                if c == "\\" and i + 1 < len(s):
                    out.append(s[i + 1])
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
                continue
            if c == "/" and i + 1 < len(s) and s[i + 1] == "/":
                j = s.find("\n", i + 2)
                i = len(s) if j == -1 else j
                continue
            if c == "/" and i + 1 < len(s) and s[i + 1] == "*":
                j = s.find("*/", i + 2)
                if j == -1:
                    raise ValueError("unterminated /* */ comment")
                i = j + 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    return strip(text)


def load_config(path: Path) -> dict:
    text = path.read_text()
    cleaned = strip_jsonc_comments(text)
    cfg = json.loads(cleaned)
    if not isinstance(cfg, dict):
        raise ValueError("config top level is not an object")
    return cfg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("token", help="Access token for the Qian Wenku MCP server")
    p.add_argument("--force", action="store_true",
                   help=f"Overwrite an existing mcp.{SERVER_KEY} entry with a different token")
    args = p.parse_args(argv)

    if PREFERRED.exists():
        path = PREFERRED
    elif FALLBACK.exists():
        path = FALLBACK
    else:
        path = PREFERRED  # default: create as .jsonc

    if path.exists():
        try:
            cfg = load_config(path)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"error: {path} could not be parsed: {e}", file=sys.stderr)
            return 2
    else:
        cfg = {"$schema": "https://opencode.ai/config.json"}

    mcp = cfg.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        print(f"error: {path} \"mcp\" key is not an object", file=sys.stderr)
        return 2

    desired = build_entry(args.token)
    existing = mcp.get(SERVER_KEY)
    if existing is not None:
        if existing == desired:
            print(f"mcp.{SERVER_KEY} is already configured correctly in {path}")
            return 0
        if not args.force:
            print(
                f"error: mcp.{SERVER_KEY} already exists in {path} "
                f"with a different token. Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1

    mcp[SERVER_KEY] = desired
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {path}")
    print(f"  mcp.{SERVER_KEY}.type = remote")
    print(f"  mcp.{SERVER_KEY}.url = {SERVER_URL}")
    print(f"  mcp.{SERVER_KEY}.headers.Authorization = Bearer <token>")
    print()
    print("Restart OpenCode for the change to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
