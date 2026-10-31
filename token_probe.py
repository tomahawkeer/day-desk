"""TOKEN_PROBE.bat — measure what each Claude Code call drags in (2026-10-31).

A one-line health check on Jeff's PC measured 429,398 input tokens: Claude Code loads its whole interactive
setup (connectors / MCP servers, skills, memory files, its coding system prompt) into every desk call. This sends
the SAME tiny health check five ways, each stripping one more layer, and prints tokens, the API-equivalent cost
and whether the answer was still right. Nothing here touches trading, the book or any money limit.

  A  as the desk runs today
  B  A + --strict-mcp-config          (load no connectors / MCP servers)
  C  B + --disable-slash-commands     (no skills or commands list)
  D  C + memory/skill env switches    (no CLAUDE.md or auto memory, no bundled skills, no git/Chrome prompt sections)
  E  D + the desk's own system prompt REPLACES Claude Code's coding prompt

Every variant uses Jeff's SUBSCRIPTION login (no API key: --bare is NOT used because it requires one).
The winner goes into config.json as "claude_lean"; nothing changes until that is written.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from brain import ClaudeCLIBrain          # noqa: E402
from day_desk import find_claude_cli, load_config, write_json   # noqa: E402

ENV_SWITCHES = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": 1, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": 1,
                "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": 1, "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": 1,
                "CLAUDE_CODE_DISABLE_CFC_PROMPT": 1, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1}
VARIANTS = [
    ("A", "as today", {}),
    ("B", "+ no connectors", {"args": ["--strict-mcp-config"]}),
    ("C", "+ no skills list", {"args": ["--strict-mcp-config", "--disable-slash-commands"]}),
    ("D", "+ no memory files", {"args": ["--strict-mcp-config", "--disable-slash-commands"], "env": ENV_SWITCHES}),
    ("E", "+ desk's own system prompt", {"args": ["--strict-mcp-config", "--disable-slash-commands"],
                                          "env": ENV_SWITCHES, "replace_system_prompt": True}),
]


def main() -> None:
    cfg = load_config(HERE)
    exe = find_claude_cli()
    if not exe:
        print("Claude Code was not found on this PC.")
        return
    print(f"Model: {cfg['model']}. Sending the same one-line health check {len(VARIANTS)} ways...\n")
    rows = []
    for key, name, lean in VARIANTS:
        b = ClaudeCLIBrain(exe, cfg["model"], lean=lean)
        try:
            reply = b.ping()
            u = b.last_usage() or {}
            ok = "DAY DESK OK" in reply.upper()
            rows.append({"variant": key, "name": name, "lean": lean, "ok": ok, **u})
            print(f"  {key} {name:28s} {'OK ' if ok else 'BAD'}  input {u.get('tok_in', 0):>9,} tokens "
                  f"(new {u.get('tok_new', 0):>7,}, cached {u.get('tok_cached', 0):>9,})  ≈${u.get('cost_usd', 0):.4f}  "
                  f"{u.get('secs', 0)}s")
        except Exception as e:
            rows.append({"variant": key, "name": name, "lean": lean, "ok": False, "error": str(e)[:300]})
            print(f"  {key} {name:28s} FAILED: {str(e)[:150]}")
    good = [r for r in rows if r.get("ok") and r.get("tok_in")]
    if good and rows[0].get("tok_in"):
        best = min(good, key=lambda r: r["tok_in"])
        print(f"\nLeanest working variant: {best['variant']} ({best['name']}): {best['tok_in']:,} input tokens vs "
              f"{rows[0]['tok_in']:,} today = {best['tok_in'] / rows[0]['tok_in'] * 100:.1f}% of today's size.")
    write_json(os.path.join(HERE, "data", "token_probe.json"), {"rows": rows}, indent=1)
    print("Saved data/token_probe.json. Tell Claude it's done.")


if __name__ == "__main__":
    main()
