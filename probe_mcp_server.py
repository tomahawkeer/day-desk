"""probe_mcp_server.py — the smallest possible MCP server, for TOKEN_PROBE_ONE_SERVER.bat (2026-11-01).

Why it exists: on anthropics/claude-code#106413, Ben-Home asked for one more measurement row. Every lean row we posted
used `--strict-mcp-config`, which loads NO MCP servers at all. That is fine for Day Desk (its calls need none), but it
does not show what happens when a script keeps the ONE server it needs and drops only the connectors:
`--strict-mcp-config --mcp-config <one server>`. To measure that we need one real server Claude Code will load.

This is that server: plain Python, standard library only, no network, no install. It speaks MCP over stdin/stdout
(one JSON-RPC message per line) and offers exactly one tool, `desk_clock`, which returns the time. Nothing in the desk
ever calls it. When Claude Code asks it for its tool list it appends a line to the --stamp file, so the probe can
PROVE the server was loaded (if the stamp is missing, the row's token count says nothing about a loaded server).

It never touches trading, the book, config.json or any money limit.
"""
from __future__ import annotations

import json
import sys
import time

SERVER_NAME = "day-desk-probe"
PROTOCOL_FALLBACK = "2025-06-18"     # used only if the client doesn't say which protocol version it speaks
TOOL = {
    "name": "desk_clock",
    "description": "Returns the current time as an ISO string. Used only by Day Desk's token probe.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def stamp(path: str | None, what: str) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {what}\n")
    except OSError:
        pass                         # a stamp problem must never break the handshake


def answer(msg: dict, stamp_path: str | None) -> dict | None:
    """The reply to one JSON-RPC message, or None for a notification (no reply allowed)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                  # notifications (e.g. notifications/initialized): never answered
        return None
    if method == "initialize":
        stamp(stamp_path, "initialize")
        ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_FALLBACK
        result = {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME, "version": "1.0"}}
    elif method == "tools/list":
        stamp(stamp_path, "tools/list")
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        stamp(stamp_path, "tools/call")
        result = {"content": [{"type": "text", "text": time.strftime("%Y-%m-%dT%H:%M:%S")}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    stamp_path = argv[argv.index("--stamp") + 1] if "--stamp" in argv else None
    stamp(stamp_path, "started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            out = answer(m, stamp_path)
            if out is not None:
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
