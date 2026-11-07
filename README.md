# Apex MCP

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude, Codex, Cursor, Gemini CLI, ...) work inside **your own running MSC Apex session** for FEA pre-processing: inspect the model, check CAD, build meshes, loads and scenarios, drive the viewport and run Apex Python.

## Features

- **Read the model:** tree, part and mesh summaries, faces, connections, loads, constraints, scenarios.
- **Check geometry:** slivers, small edges and faces, gaps and cracks, reported without repairs.
- **Build models:** materials, solid properties, meshing, glue, RBE2, beams, constraints, nodal loads and static scenarios.
- **Check decks:** exports a BDF and returns a compact summary with lint warnings.
- **Drive the viewport:** visibility, views, highlights, section cuts and screenshots.
- **Run Apex Python:** with an API lookup and scripting guide so the assistant writes correct calls.

## Requirements

- Windows
- MSC Apex 2024.1
- Python 3.10+
- git (optional; without it the skills are downloaded as a zip)

## Installation

1. **Clone and set up**

   ```bat
   git clone https://github.com/rehman-saif/apex-mcp.git
   cd apex-mcp
   setup.bat
   ```

   `setup.bat` creates a `.venv`, writes `config.json` with a fresh access token, downloads the
   [nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills) into `vendor\`, and
   writes ready-to-paste client config snippets. If Claude Code or Codex is installed, it offers
   to register the server for you (`setup.bat /y` skips the questions).

2. **Add the palette to Apex**

   In Apex: **Options → Application Settings → Custom Tools**, add `<this folder>\apex_palette`.
   Restart Apex if the **MCP** palette doesn't appear.

3. **Connect your MCP client**

   | Client | How |
   |---|---|
   | Claude Code | Done by `setup.bat`, or `claude mcp add --scope user apex -- <python> <server.py>` |
   | Claude Desktop | Merge `mcp_config.snippet.json` into `%APPDATA%\Claude\claude_desktop_config.json` |
   | Cursor | Merge `mcp_config.snippet.json` into `%USERPROFILE%\.cursor\mcp.json` |
   | Gemini CLI | Merge `mcp_config.snippet.json` into `%USERPROFILE%\.gemini\settings.json` |
   | Codex CLI | Done by `setup.bat`, or append `codex_config.snippet.toml` to `%USERPROFILE%\.codex\config.toml` (keep `tool_timeout_sec = 300`) |

   Restart the client after editing its config.

Re-run `setup.bat` at any time to update the skills.

## Usage

1. Open Apex and your model.
2. Open **MCP → MCP Bridge** and press **Start**. Press **Self-test** once to confirm it works.
3. Ask your assistant, for example:
   - *"Summarise the parts in my model and check the geometry for faults."*
   - *"Mesh the bracket with 3 mm tets, constrain the bolt holes and apply 500 N on the top face."*
   - *"Export the static scenario and tell me if the deck has problems."*
   - *"Hide everything except the housing and take a screenshot."*
4. Press **Stop** when you're done.

The bridge serves one client at a time; close other sessions connected to the same Apex first.

## How it works

```
MCP client ──stdio──> apex_mcp/server.py        (your Python 3.10+ venv)
                         │  JSON over 127.0.0.1 + token, one call at a time
                         ▼
MSC Apex ── MCP palette ── "MCP Bridge" tool    (listener inside Apex)
                         │  apex_sdk.runScriptFunction
                         ▼
             Apex script thread → apex_mcp/worker  (runs the actual Apex API calls)
```

- The **MCP server** runs outside Apex and speaks MCP to your client over stdio.
- The **MCP Bridge** tool runs inside Apex and listens on localhost. Each request is handed to
  Apex's script thread, where the **worker** calls the Apex API and returns compact JSON.
- Apex has a single script thread that shares memory with your model, so calls run one at a time.
  Queries never walk individual elements or nodes. They use bulk arrays, sampling, time budgets,
  item caps and response-size limits, so even large models stay responsive.
- For Apex Python, the server serves the `apex-scripting` skill: exact API signatures
  (`apex_api_lookup`) and scripting rules (`apex_scripting_guide`). The first `run_apex_python`
  of a session waits until one of them has been consulted.

## Tools

| Group | Tools |
|---|---|
| Session | `apex_status`, `reconnect` |
| Model (read-only) | `list_tree`, `part_summary`, `geometry_check`, `get_entity`, `find_entities`, `list_faces`, `list_connections`, `list_loads_constraints`, `list_catalog`, `list_scenarios` |
| Model building | `import_fe_model`, `create_material`, `assign_solid_property`, `mesh_solid`, `glue`, `rbe2`, `beam`, `constrain`, `nodal_load`, `static_scenario` |
| Deck | `export_bdf` |
| Viewport | `visibility`, `set_view`, `display_options`, `highlight`, `annotate`, `cut_view`, `screenshot` |
| Apex API help | `apex_api_lookup`, `apex_scripting_guide` |
| Raw | `run_apex_python` |

Builder tools are idempotent: calling one again with the same name replaces what it made before.

## Configuration

Settings live in `config.json` (created by `setup.bat` from `config.example.json`, not committed).

| Key | Meaning |
|---|---|
| `bridge_port` / `bridge_token` | Where the bridge listens and its shared secret |
| `script_unit_system` | e.g. `mm-t-s-N-K`; empty leaves Apex's units alone |
| `output_dir` | Screenshots, saved scripts, exported decks and logs |
| `call_timeout_s` | Maximum wait for one Apex call (default 300) |
| `query_time_budget_s` / `max_items` / `max_response_kb` | Limits that keep queries small and fast |
| `require_skill_lookup` | Require an API lookup before the first raw script (default `true`) |

Most changes apply on the next call; `bridge_port`, `bridge_token` and `call_timeout_s` apply when
you press **Start** again.

## Security

- The bridge listens on `127.0.0.1` only and every connection must present the token.
- Only the model-building tools and `run_apex_python` change the model. Every raw script is saved to `output\scripts\`.
- Logs are written to `output\bridge\`.

## Development

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python tests\test_units.py
.venv\Scripts\python tests\test_bridge.py
.venv\Scripts\python tests\test_end_to_end.py
```

The end-to-end test runs the full chain (client → server → bridge → worker) against a fake Apex
and fails if any tool iterates elements. Worker edits are picked up on the next call without
restarting Apex; `server.py` edits need an MCP client restart.

## License

[MIT](LICENSE)

## Disclaimer

This is an independent, personal project. It is **not affiliated with, endorsed by, or supported
by MSC Software Corporation or Hexagon AB**. MSC Apex and MSC Nastran are trademarks of their
respective owners and are used here only to describe compatibility. You need your own licensed
copy of MSC Apex. Use at your own risk, and always review changes an AI assistant makes to your
model.
