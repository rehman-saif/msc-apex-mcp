"""Run by setup.bat from the venv. Idempotent; safe to re-run to update.

1. config.json  - created from config.example.json with a fresh bridge_token; keys added in
                  newer versions are merged in without touching existing values.
2. snippets     - MCP client config with this folder's real paths (Claude Desktop, Cursor,
                  Gemini CLI: mcp_config.snippet.json; Codex CLI: codex_config.snippet.toml).
3. --codex      - installs the fea-structural-help plugin (nastran-fea-skills) in Codex CLI and
                  registers this server (`codex plugin add`, `codex mcp add`).
4. --claude-code - installs the fea-structural-help plugin (all five skills) and registers
                  this server, both at user scope.
The server itself does not read the skills: the client loads them. For other clients see
https://github.com/rehman-saif/nastran-fea-skills.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARKETPLACE = "rehman-saif/nastran-fea-skills"
PLUGIN = "fea-structural-help"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SERVER = os.path.join(ROOT, "apex_mcp", "server.py")


RETIRED = ("skills_dir", "require_skill_lookup", "script_preflight", "max_highlight_ids")


def ensure_config():
    path = os.path.join(ROOT, "config.json")
    with open(os.path.join(ROOT, "config.example.json"), "r", encoding="utf-8") as fh:
        example = json.load(fh)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        added = [k for k in example if k not in cfg]
        for k in added:
            cfg[k] = example[k]
        gone = [k for k in RETIRED if k in cfg]  # settings of features this version no longer has
        for k in gone:
            del cfg[k]
        msg = "config.json kept" + (" (added: %s)" % ", ".join(added) if added else "") + \
              (" (removed: %s)" % ", ".join(gone) if gone else "")
    else:
        cfg = dict(example)
        msg = "config.json created with a new bridge_token"
    if not cfg.get("bridge_token"):
        cfg["bridge_token"] = secrets.token_hex(16)
    if not cfg.get("output_dir"):
        cfg["output_dir"] = os.path.join(ROOT, "output")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print("OK:", msg)


def write_snippets():
    entry = {"command": PY, "args": [SERVER]}
    with open(os.path.join(ROOT, "mcp_config.snippet.json"), "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"apex": entry}}, fh, indent=2)
    with open(os.path.join(ROOT, "codex_config.snippet.toml"), "w", encoding="utf-8") as fh:
        fh.write("[mcp_servers.apex]\ncommand = '%s'\nargs = ['%s']\ntool_timeout_sec = 300\n" % (PY, SERVER))
    print("OK: client config written to mcp_config.snippet.json and codex_config.snippet.toml")


def register_claude_code():
    claude = shutil.which("claude")
    if not claude:
        print("SKIP: 'claude' is not on PATH, so Claude Code was not configured")
        return
    r = subprocess.run([claude, "plugin", "install", PLUGIN, "--marketplace", MARKETPLACE, "--scope", "user"])
    print("OK: %s plugin installed" % PLUGIN if r.returncode == 0 else
          "WARNING: plugin install failed; in Claude Code run /plugin install %s --marketplace %s"
          % (PLUGIN, MARKETPLACE))
    if subprocess.run([claude, "mcp", "get", "apex"], capture_output=True).returncode == 0:
        print("OK: 'apex' MCP server already registered in Claude Code")
        return
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", "apex", "--", PY, SERVER])
    print("OK: 'apex' MCP server registered in Claude Code" if r.returncode == 0 else
          "WARNING: could not register the MCP server; see mcp_config.snippet.json")


def register_codex():
    codex = shutil.which("codex")
    if not codex:
        print("SKIP: 'codex' is not on PATH, so Codex was not configured")
        return
    r = subprocess.run([codex, "plugin", "marketplace", "add", MARKETPLACE])
    if r.returncode == 0:  # also fine when already added; upgrade refreshes the snapshot
        subprocess.run([codex, "plugin", "marketplace", "upgrade", MARKETPLACE.split("/")[1]],
                       capture_output=True)
        r = subprocess.run([codex, "plugin", "add", "%s@%s" % (PLUGIN, MARKETPLACE.split("/")[1])])
    print("OK: %s plugin installed in Codex" % PLUGIN if r.returncode == 0 else
          "WARNING: Codex plugin install failed; run: codex plugin marketplace add %s && "
          "codex plugin add %s@%s" % (MARKETPLACE, PLUGIN, MARKETPLACE.split("/")[1]))
    if subprocess.run([codex, "mcp", "get", "apex"], capture_output=True).returncode == 0:
        print("OK: 'apex' MCP server already registered in Codex")
        return
    r = subprocess.run([codex, "mcp", "add", "apex", "--", PY, SERVER])
    print("OK: 'apex' MCP server registered in Codex (add tool_timeout_sec = 300 under "
          "[mcp_servers.apex] in ~/.codex/config.toml; see codex_config.snippet.toml)" if r.returncode == 0 else
          "WARNING: could not register the MCP server; append codex_config.snippet.toml to ~/.codex/config.toml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-code", action="store_true", help="also configure Claude Code")
    ap.add_argument("--codex", action="store_true", help="also register the server with Codex CLI")
    a = ap.parse_args()
    ensure_config()
    write_snippets()
    if a.claude_code:
        register_claude_code()
    if a.codex:
        register_codex()


if __name__ == "__main__":
    main()
