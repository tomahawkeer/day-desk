"""TOKEN_PROBE.bat — measure what each Claude Code call drags in (2026-10-31).

CORRECTED 10-31 evening: every variant below runs with the desk's `--tools ""`, which also removes ToolSearch, so all
MCP/connector tools load upfront; that, not plain `claude -p` (~60k), is what variant A measured.
A one-line health check on tomahawkeer's PC measured 429,398 input tokens: Claude Code loads its whole interactive
setup (connectors / MCP servers, skills, memory files, its coding system prompt) into every desk call. This sends
the SAME tiny health check five ways, each stripping one more layer, and prints tokens, the API-equivalent cost
and whether the answer was still right. Nothing here touches trading, the book or any money limit.

  A  as the desk runs today
  B  A + --strict-mcp-config          (load no connectors / MCP servers)
  C  B + --disable-slash-commands     (no skills or commands list)
  D  C + memory/skill env switches    (no CLAUDE.md or auto memory, no bundled skills, no git/Chrome prompt sections)
  E  D + the desk's own system prompt REPLACES Claude Code's coding prompt

Every variant uses tomahawkeer's SUBSCRIPTION login (no API key: --bare is NOT used because it requires one).
The winner goes into config.json as "claude_lean"; nothing changes until that is written.

ADDED 11-01 (TOKEN_PROBE_ONE_SERVER.bat, promised on anthropics/claude-code#106413 to Ben-Home): three more rows.
  F  B + --mcp-config <one server>     (keep ONE MCP server, drop the connectors: Ben-Home's missing row)
  G  E + --mcp-config <one server>     (the same on the desk's fully lean setup)
  H  A + ENABLE_CLAUDEAI_MCP_SERVERS=false   (the documented switch that drops only the claude.ai connectors and
                                               keeps any MCP servers tomahawkeer has configured himself)
The one server is probe_mcp_server.py (one tool, stdlib only, no network). It writes a stamp file when Claude Code
asks for its tool list, so each F/G row says whether the server was really loaded. Every row also saves the EXACT
command line and env switches it ran with (ledger H-194: quote the exact command behind any published number).
`python token_probe.py B E F G H` runs only those rows (the bat does this: row A costs ~429k tokens and is known).
Results of a partial run go to data/token_probe_one_server.json; the full A-E run still writes token_probe.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from brain import ClaudeCLIBrain, claude_workdir          # noqa: E402
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
# 11-01: extra rows, run only when named (TOKEN_PROBE_ONE_SERVER.bat). Kept OUT of VARIANTS on purpose: the plain
# TOKEN_PROBE.bat run stays exactly A-E, and VARIANTS[-1] stays the measured winner that config.json must equal.
EXTRA_VARIANTS = [
    # "{ONE}" is replaced with the path of a generated --mcp-config file naming probe_mcp_server.py only.
    ("F", "B + one MCP server (Ben-Home)", {"args": ["--strict-mcp-config", "--mcp-config", "{ONE}"]}),
    ("G", "E + one MCP server", {"args": ["--strict-mcp-config", "--disable-slash-commands", "--mcp-config", "{ONE}"],
                                 "env": ENV_SWITCHES, "replace_system_prompt": True}),
    ("H", "A + connectors off by env", {"env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}}),
]
ONE_SERVER_ROWS = {"F", "G"}
SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_mcp_server.py")   # beside this file


def one_server_config(folder: str, stamp: str) -> str:
    """Write an --mcp-config file naming exactly one server: probe_mcp_server.py, run by this same Python."""
    path = os.path.join(folder, "probe-one-server.json")
    cfg = {"mcpServers": {"day-desk-probe": {"type": "stdio", "command": sys.executable,
                                             "args": [SERVER_SCRIPT, "--stamp", stamp]}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    return path


def resolve(lean: dict, one: str) -> dict:
    """The variant with "{ONE}" swapped for the real config path."""
    out = dict(lean)
    if "args" in lean:
        out["args"] = [one if a == "{ONE}" else a for a in lean["args"]]
    return out


class CommandRecorder:
    """Wraps subprocess.run for the duration of the probe and keeps the exact argv + the env switches each call
    used, so a number posted publicly can quote the exact command that produced it. Prompts are not kept."""

    def __init__(self):
        self.last: dict | None = None
        self.switches: set[str] = set()      # only the env switches the ROW sets are recorded: the inherited
        self._real = subprocess.run          # environment can hold account ids and must never land in a posted file

    def __enter__(self):
        def run(args, *a, **k):
            env = k.get("env") or {}
            self.last = {"argv": [str(x) for x in args],
                         "env_switches": {e: env[e] for e in sorted(self.switches) if e in env}}
            return self._real(args, *a, **k)
        subprocess.run = run
        return self

    def __exit__(self, *exc):
        subprocess.run = self._real
        return False

    def command_line(self) -> str:
        if not self.last:
            return ""
        raw = self.last["argv"][1:]
        # File paths after these flags hold the Windows user folder: never in a file meant to be posted publicly.
        hide = {"--mcp-config": "<one-server.json>", "--system-prompt-file": "<desk-system-prompt.txt>"}
        raw = [hide[raw[i - 1]] if i and raw[i - 1] in hide else x for i, x in enumerate(raw)]
        argv = ["claude"] + [('""' if x == "" else (f'"{x}"' if " " in x else x)) for x in raw]
        envs = " ".join(f"{k}={v}" for k, v in self.last["env_switches"].items())
        return (envs + " " if envs else "") + " ".join(argv) + "   (prompt on stdin)"


def pick(argv: list[str]) -> list[tuple]:
    """No names = the original A-E run. Names = just those rows, in A..H order."""
    want = [a.strip().upper() for a in argv if a.strip()]
    if not want:
        return list(VARIANTS)
    every = VARIANTS + EXTRA_VARIANTS
    known = {k for k, _, _ in every}
    bad = [w for w in want if w not in known]
    if bad:
        raise SystemExit(f"Unknown row(s): {', '.join(bad)}. Rows: {', '.join(sorted(known))}")
    return [v for v in every if v[0] in want]


def main(argv: list[str] | None = None) -> None:
    argv = [] if argv is None else argv            # the command line is passed in by __main__ only
    chosen = pick(argv)
    cfg = load_config(HERE)
    exe = find_claude_cli()
    if not exe:
        print("Claude Code was not found on this PC.")
        return
    try:
        cli_version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        cli_version = f"unknown ({e})"
    work = claude_workdir()
    print(f"Claude Code {cli_version}. Model: {cfg['model']}. "
          f"Sending the same one-line health check {len(chosen)} ways ({', '.join(k for k, _, _ in chosen)})...\n")
    rows = []
    with CommandRecorder() as rec:
        for key, name, lean in chosen:
            stamp = os.path.join(work, f"probe-stamp-{key}-{int(time.time())}.txt")
            one = one_server_config(work, stamp) if key in ONE_SERVER_ROWS else ""
            real = resolve(lean, one)
            b = ClaudeCLIBrain(exe, cfg["model"], lean=real)
            rec.last = None
            rec.switches = set((real.get("env") or {}))
            row = {"variant": key, "name": name, "lean": lean}
            try:
                reply = b.ping()
                u = b.last_usage() or {}
                row.update(ok="DAY DESK OK" in reply.upper(), **u)
            except Exception as e:
                row.update(ok=False, error=str(e)[:300])
            row["command"] = rec.command_line()
            if key in ONE_SERVER_ROWS:
                seen = open(stamp, encoding="utf-8").read() if os.path.exists(stamp) else ""
                row["server_loaded"] = "tools/list" in seen
                row["server_stamp"] = seen.split()[1::2]
            rows.append(row)
            loaded = ("" if key not in ONE_SERVER_ROWS else
                      ("  [server LOADED]" if row["server_loaded"] else "  [server NOT loaded - row says nothing]"))
            if row.get("error"):
                print(f"  {key} {name:30s} FAILED: {row['error'][:150]}{loaded}")
            else:
                print(f"  {key} {name:30s} {'OK ' if row['ok'] else 'BAD'}  input {row.get('tok_in', 0):>9,} tokens "
                      f"(new {row.get('tok_new', 0):>7,}, cached {row.get('tok_cached', 0):>9,})  "
                      f"≈${row.get('cost_usd', 0):.4f}  {row.get('secs', 0)}s{loaded}")
    full = chosen[0][0] == "A"                     # the leanest-vs-today line needs row A as "today"
    good = [r for r in rows if r.get("ok") and r.get("tok_in")]
    if full and good and rows[0].get("tok_in"):
        best = min(good, key=lambda r: r["tok_in"])
        print(f"\nLeanest working variant: {best['variant']} ({best['name']}): {best['tok_in']:,} input tokens vs "
              f"{rows[0]['tok_in']:,} today = {best['tok_in'] / rows[0]['tok_in'] * 100:.1f}% of today's size.")
    print("\nExact command per row (saved in the file too):")
    for r in rows:
        print(f"  {r['variant']}: {r.get('command') or '(not run)'}")
    if any(k in ONE_SERVER_ROWS for k, _, _ in chosen):
        print('\n<one-server.json> = {"mcpServers": {"day-desk-probe": {"type": "stdio", "command": "python", '
              '"args": ["probe_mcp_server.py"]}}}   (one tool, desk_clock; stdlib only)')
    out = "token_probe.json" if not argv else "token_probe_one_server.json"
    write_json(os.path.join(HERE, "data", out),
               {"t": time.time(), "claude_code": cli_version, "model": cfg["model"], "rows": rows}, indent=1)
    print(f"\nSaved data/{out}. Tell Claude it's done.")


if __name__ == "__main__":
    main(sys.argv[1:])
