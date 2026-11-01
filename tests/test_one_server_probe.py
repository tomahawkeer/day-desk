"""TOKEN_PROBE_ONE_SERVER.bat (11-01, promised on anthropics/claude-code#106413): probe_mcp_server.py speaks enough
MCP for Claude Code to load it, and token_probe.py's rows F/G/H build the right command lines, prove whether the
one server was loaded, save the exact command, and never touch the original A-E results. No network, no Claude:
a fake `claude` script stands in and really launches the server from the generated --mcp-config file."""
import json
import os
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import token_probe                                               # noqa: E402

SERVER = os.path.join(ROOT, "probe_mcp_server.py")


def talk(msgs, stamp):
    p = subprocess.run([sys.executable, SERVER, "--stamp", str(stamp)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=20)
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def test_server_handshake_lists_one_tool_and_stamps(tmp_path):
    st = tmp_path / "stamp.txt"
    out = talk([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desk_clock"}},
                {"jsonrpc": "2.0", "id": 4, "method": "nope"}], st)
    assert [o["id"] for o in out] == [1, 2, 3, 4]                  # the notification got no reply
    assert out[0]["result"]["protocolVersion"] == "2025-06-18" and "tools" in out[0]["result"]["capabilities"]
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["desk_clock"]
    assert out[2]["result"]["content"][0]["type"] == "text"
    assert out[3]["error"]["code"] == -32601
    assert "tools/list" in st.read_text()


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, subprocess, sys
a = sys.argv[1:]
if a == ["--version"]:
    print("9.9.9 (fake)"); sys.exit(0)
sys.stdin.read()
import os
if "--mcp-config" in a and not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ignore_mcp")):
    cfg = json.load(open(a[a.index("--mcp-config") + 1]))
    for s in cfg["mcpServers"].values():
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        subprocess.run([s["command"]] + s["args"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=20)
tok = 2854 + (300 if "--mcp-config" in a else 0)
print(json.dumps({"type": "result", "result": '{"ok": true, "says": "DAY DESK OK"}', "total_cost_usd": 0.001,
                  "num_turns": 1, "duration_ms": 900, "usage": {"input_tokens": tok, "output_tokens": 20}}))
'''


def fake_cli(tmp_path, ignore_mcp=False):
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE)
    if ignore_mcp:
        (tmp_path / "ignore_mcp").write_text("x")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


@pytest.fixture
def probe_home(tmp_path, monkeypatch):
    home = tmp_path / "desk"
    (home / "data").mkdir(parents=True)
    import shutil
    shutil.copy(os.path.join(ROOT, "config.json"), home / "config.json")
    (home / "data" / "token_probe.json").write_text('{"rows": "ORIGINAL A-E RESULT"}')
    monkeypatch.setattr(token_probe, "HERE", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(token_probe, "claude_workdir", lambda: str(work))
    return home


@pytest.mark.skipif(os.name == "nt", reason="the fake claude is a shebang script")
def test_one_server_rows_prove_the_server_loaded_and_save_exact_commands(probe_home, tmp_path, monkeypatch):
    monkeypatch.setattr(token_probe, "find_claude_cli", lambda: fake_cli(tmp_path))
    token_probe.main(["B", "E", "F", "G", "H"])
    out = json.load(open(probe_home / "data" / "token_probe_one_server.json"))
    rows = {r["variant"]: r for r in out["rows"]}
    assert list(rows) == ["B", "E", "F", "G", "H"] and out["claude_code"] == "9.9.9 (fake)"
    assert all(r["ok"] for r in rows.values())
    assert rows["F"]["server_loaded"] and rows["G"]["server_loaded"]
    assert "--strict-mcp-config --mcp-config" in rows["F"]["command"] and "{ONE}" not in rows["F"]["command"]
    assert rows["F"]["tok_in"] == 2854 + 300 and rows["B"]["tok_in"] == 2854
    assert rows["H"]["command"].startswith("ENABLE_CLAUDEAI_MCP_SERVERS=false ")
    assert "--strict-mcp-config" not in rows["H"]["command"]
    assert all('--tools ""' in r["command"] and "--bare" not in r["command"] for r in rows.values())
    assert "CLAUDE_CODE_DISABLE_CLAUDE_MDS=1" in rows["G"]["command"] and "--system-prompt-file" in rows["G"]["command"]
    assert json.load(open(probe_home / "data" / "token_probe.json")) == {"rows": "ORIGINAL A-E RESULT"}
    assert rows["B"]["command"].startswith("claude ")                # nothing inherited from the environment
    monkeypatch.setenv("CLAUDE_CODE_ACCOUNT_UUID", "secret-id")
    token_probe.main(["G"])
    g = json.load(open(probe_home / "data" / "token_probe_one_server.json"))["rows"][0]
    assert "secret-id" not in json.dumps(g)


@pytest.mark.skipif(os.name == "nt", reason="the fake claude is a shebang script")
def test_a_server_that_never_loaded_is_reported_as_not_loaded(probe_home, tmp_path, monkeypatch):
    monkeypatch.setattr(token_probe, "find_claude_cli", lambda: fake_cli(tmp_path, ignore_mcp=True))
    token_probe.main(["F"])
    row = json.load(open(probe_home / "data" / "token_probe_one_server.json"))["rows"][0]
    assert row["variant"] == "F" and row["server_loaded"] is False


def test_unknown_row_names_are_refused():
    with pytest.raises(SystemExit):
        token_probe.pick(["Z"])
    assert [v[0] for v in token_probe.pick([])] == ["A", "B", "C", "D", "E"]      # plain run unchanged
