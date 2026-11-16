# Apex MCP

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude, Codex, Cursor, Gemini CLI, ...) work inside **your own running MSC Apex session** for FEA pre-processing. It is a bridge with nine tools: the assistant reads the model, looks at it, and runs Apex Python in it.

## Features

- **Read the model:** parts with their counts, connections, loads, constraints, catalog and scenarios in one
  overview; part and mesh summaries; faces by shape and position.
- **Look at it:** isolate or hide parts, colour them, take several views in one labelled screenshot.
- **Run Apex Python:** a read-only script tool that refuses anything that would change the model, and one for
  changes whose result says what it changed. Tested helpers cover the steps of a linear static model (material,
  PSOLID, tet mesh, glue, RBE2, beam, constraint, nodal load, SOL 101 scenario) and a CAD health check.
- **See what this Apex has:** `apex_describe` reads modules, signatures, enum values and an entity's properties
  from the running Apex.
- **Export decks:** writes the Nastran BDF of a scenario, ready for the deck checks of the
  `nastran-diagnostics` skill.

The server is only the bridge to Apex. The documentation an assistant needs (Apex API signatures and scripting
rules, Apex GUI help, Nastran reference, deck and run diagnostics) is in the companion
[nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills), which the assistant loads as skills.

## Requirements

- Windows
- MSC Apex 2024.1
- Python 3.10+
- Recommended: the [nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills) in your MCP client

## Installation

1. **Clone and set up**

   ```bat
   git clone https://github.com/rehman-saif/apex-mcp.git
   cd apex-mcp
   setup.bat
   ```

   `setup.bat` creates a `.venv`, writes `config.json` with a fresh access token and writes
   ready-to-paste client config snippets. If Claude Code or Codex is installed, it offers to
   install the skills plugin and register the server for you (`setup.bat /y` skips the questions).

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

4. **Add the skills to your client**

   `setup.bat` does this for Claude Code and Codex. For other clients follow the
   [nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills) instructions (Gemini CLI
   extension, `AGENTS.md` for Cursor and others). Without them the tools work, but the assistant
   writes Apex Python from memory, which is usually wrong in the details.

## Usage

1. Open Apex and your model.
2. Open **MCP → MCP Bridge** and press **Start**. Press **Self-test** once to confirm it works.
3. Ask your assistant, for example:
   - *"Summarise the parts in my model and check the geometry for faults."*
   - *"What connections, loads and scenarios does this model have?"*
   - *"Mesh the bracket with 3 mm tets, constrain the bolt holes and apply 500 N on the top face."*
   - *"Export the static scenario and tell me if the deck has problems."*
   - *"Show only the housing, colour each part differently and give me iso, front and top views."*
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
- The server has few tools on purpose. What the summaries do not cover is a script, with helpers
  for the common steps, so the tool list stays small (about 4k tokens in the assistant's context).
- The server carries no documentation. The client's `apex-scripting` skill gives API signatures
  and scripting rules; `apex_describe` reads what the running Apex really has.

## Tools

| Tool | Does |
|---|---|
| `apex_status` | Checks the connection: server version, Apex release, model, unit systems |
| `model_overview` | Parts (one row each with solid, surface, mesh, element and node counts), connections, loads, constraints, catalog, scenarios; pick `sections` to get less |
| `part_summary` | Meshes, sampled element properties, solids and surfaces of the given parts |
| `list_faces` | Faces by kind (plane, cylinder), position or normal, to target loads, constraints and ties |
| `screenshot` | Pictures of the viewport, with an optional scene and several views |
| `apex_describe` | What the running Apex has: a module, a signature, enum values, an entity's properties |
| `query_apex_python` | Runs a script that only reads |
| `run_apex_python` | Runs a script that changes the model or the view |
| `export_bdf` | Writes a scenario's Nastran deck and returns its path |

Every tool carries MCP annotations: `apex_status`, `model_overview`, `part_summary`, `list_faces`, `apex_describe`
and `query_apex_python` are marked read-only, `screenshot` and `export_bdf` as non-destructive, and
`run_apex_python` as destructive. To stop being asked before every read, allow the read tools in your client, e.g.
in Claude Code's `settings.json`:

```json
{"permissions": {"allow": ["mcp__apex__model_overview", "mcp__apex__part_summary", "mcp__apex__list_faces",
                           "mcp__apex__apex_describe", "mcp__apex__query_apex_python"]}}
```

### Screenshots

`screenshot(scene, views, layout, crop, margin, max_width, restore, save_view)`. The **scene** (everything
optional):

```jsonc
{
  "show_only": ["base/Assembly/Housing"],   // or show / hide / show_all: parts, assemblies, solids, surfaces
  "color_by_part": true,                    // parts often share one colour
  "render_style": "ShadedWithEdges",
  "transparency": {"<path>": 40},
  "mesh_edges": false, "loads_bc_markers": true, "connection_markers": true
}
```

- It applies the scene, takes up to 9 views and puts your scene and camera back. `restore: false` leaves the scene
  and the last view in place: that is how the assistant changes what you see.
- **Views:** presets `iso1`-`iso4`, `x`, `-x`, `y`, `-y`, `z`, `-z` and `right` (+X), `left` (-X), `back` (+Y), `front`
  (-Y), `top` (+Z), `bottom` (-Z), named for the side the camera is on, optionally with `zoom`. Apex fits a preset
  to what is visible, so `show_only` a part to frame it. For any other angle set the view up in Apex and have it
  stored with `save_view`; the name then works as a view.
- `layout: "sheet"` (default) returns one labelled grid, `"separate"` one image each.

How it behaves, measured on Apex 2024.1:

- Apex's `captureImage` grabs **screen pixels**, so a window on top of Apex ends up in the picture. Before each capture
  the tool checks whether anything covers Apex's window and raises it (without taking keyboard focus); an always-on-top
  window cannot be raised over and is reported as an error. Set `screenshot_bring_to_front` to `false` to get an error
  instead of raising the window.
- An empty or unreadable capture (for example with Apex's UI hidden, where Apex itself writes a 0-byte file) is retried
  and then reported.
- `crop: "model"` (default) removes the floating panels by comparing against a frame with everything hidden and cropping
  to the model; where the model runs under a panel the panel stays. `margin` (default 0.85) zooms presets out a little
  so the model clears the panels; `1.0` is Apex's tight fit.
- Visibility changes use parts and entities, not `session.hideAll()`, and are restored exactly (assemblies, parts, and
  the entities inside them). The marker and mesh-edge toggles cannot be read back from Apex, so a screenshot lists
  them under `not_restored`.
- There is no free rotation, pan or camera-position input: the camera's coordinates and parallel scale are not model
  units in Apex, and rotating about the focal point throws a part out of frame.

### Running Apex Python

Two tools run a script on Apex's script thread, with the same namespace (variables persist between them):

- **`query_apex_python(code, force)`** for reading: counts, measurements, ids, property checks. It is refused before
  it runs if it would change the model or the scene, so it can be allowed without a prompt.
- **`run_apex_python(code, force, effects)`** for changes.

The result is plain text: what the script printed, `result=<JSON>` if it set `result`, and then:

- **Effects.** A snapshot before and after the script (parts, assemblies, solid / surface / curve / mesh / element /
  node counts per part, loads, constraints, connections, materials, properties, scenarios) gives a line like
  `effects={"changed":{"base/Bracket/Part 4":{"meshes":[0,1],"elements":[0,7501],"nodes":[0,1890]}}}`, or
  `effects: none`. A script that fails halfway reports what it changed before the error. Values, assignments,
  coordinates and visibility are not tracked; a rename shows as a deletion plus a creation. Both snapshots took
  0.1 s on a small meshed model; `effects_budget_s` caps them on large ones. `effects=False` skips them.
- **Errors.** When the script raises, the output names the failing line and the signature this Apex exposes for the
  `apex` call that failed on it, resolved in the script's own frame (so `part.update(...)` is reported as
  `Part.update`). Where Apex hides the argument names it points to the skill's API file.

**What is checked before a script runs** (in the server, nothing is executed):

- a syntax error;
- a loop over `.elements` / `.nodes`: walking element objects exhausted Apex's memory on a 90k-element model.
  `force=True` runs it anyway, for a collection known to be small.

Unknown `apex` names and wrong arguments are not checked beforehand: Apex reports them at the call. An earlier
version checked them against the running Apex's API; over 316 saved scripts that caught 4 of the 39 that failed
and wrongly refused 2 that ran, so it was removed.

**What makes a query read-only.** Apex names say what they do: functions and methods named `get...`, `is...`,
`as...`, `evaluate...`, `measure...`, `find...` read (`...Fix...` repairs geometry unless called with
`autoFix=False`); the rest (`create...`, `set...`, `delete...`, `assign...`, `update`, `hide`, `display.render`,
`undo` ...) change the model, the scene or the session. `query_apex_python` refuses, and `force=True` cannot override:

- a call of an `apex` function that is not a read, also through an alias (`f = apex.deleteEntities; f(...)`);
- a method with a changing verb; `x.update(...)` unless `x` is a dict the script builds itself (`{}`, `dict()`);
- the model-building helpers `B.*`.

This guards against mistakes; it is not a sandbox, and it does not look at files or processes. So a query is always
snapshotted (even with `script_effects` off), and if it changed the model anyway the result starts with
`WARNING: this query CHANGED THE MODEL` and the effects.

### Helpers in the script namespace

Scripts get `apex` and two helper modules. `apex_describe('$Q')` and `apex_describe('$B')` list them, and
`apex_describe('$B.rbe2')` gives one's arguments and documentation.

- **`Q`, reads that never walk elements:** `part_summary`, `mesh_summary`, `topology_counts`, `sample_elements`,
  `solid_summary`, `surface_summary`, `bbox`, `faces_by_id`, `face_shape`, `nodes_on_face`, `nodes_where`,
  `nodes_by_id`, `node_near`, `rle`, `Budget`, and `geometry_check(parts, tolerance)`: Apex's geometry faults counted
  by type (sliver, small edge, small face, gap, crack ...) and small features, without repairing anything.
- **`B`, steps of a linear static model:** `create_material`, `assign_solid_property`, `mesh_solid`, `glue`, `rbe2`,
  `beam`, `constrain`, `nodal_load`, `static_scenario`. Each is idempotent: calling it again with the same name
  replaces what it made before. Targets are a pathName, `{"path": part, "faces": [ids from list_faces]}` or
  `{"path": part, "nodes": [ids]}`.

```python
B.create_material(name="Ti64", E=113800.0, nu=0.342, rho=4.43e-9)
B.assign_solid_property(paths=["base/Bracket"], material="Ti64")
result = B.mesh_solid(paths=["base/Bracket"], size=3.0)
```

### Describing the live API

The API reference in the `apex-scripting` skill is generated documentation, and Apex 2024.1 differs from it in
places (classes in other modules, enum members it leaves out, undocumented functions). `apex_describe(target,
filter, values, limit)` reads the running Apex. Apex's objects carry no docstrings, so it gives names, signatures
and values, not descriptions:

| `target` | Returns |
|---|---|
| `apex.mesh` | functions, classes, enums |
| `apex.mesh.createSolidMesh`, `apex.Part.update` | the signature with types, where Apex exposes one |
| `apex.mesh.SurfaceMeshElementShape` | members with their values |
| `apex.Part` | properties and methods (inherited ones too, via the runtime class hierarchy) |
| an entity pathName, e.g. `base/Assembly/Part 4` | its class and current property values, e.g. bounding box, colour, counts of solids and meshes |
| `$part`, `$res.meshes` | a variable left by a script |
| `$Q`, `$B`, `$B.rbe2` | the script helpers, and one helper's arguments and documentation |

Properties that can hold every element or node (`elements`, `nodes`, `...Entities`, `...IDs`) are never read.

## Configuration

Settings live in `config.json` (created by `setup.bat` from `config.example.json`, not committed).

| Key | Meaning |
|---|---|
| `bridge_port` / `bridge_token` | Where the bridge listens and its shared secret |
| `script_unit_system` | e.g. `mm-t-s-N-K`; empty leaves Apex's units alone |
| `output_dir` | Screenshots, saved views, saved scripts, exported decks and logs |
| `call_timeout_s` | Maximum wait for one Apex call (default 300) |
| `script_effects` | Effects receipt on or off (default `true`) |

The limits have defaults that rarely need changing; add a key to `config.json` only to override it:
`query_time_budget_s` (20), `max_items` (200), `max_response_kb` (256), `effects_budget_s` (5),
`effects_mesh_counts` (`true`), `screenshot_max_width` (1400), `screenshot_bring_to_front` (`true`).

Most changes apply on the next call; `bridge_port`, `bridge_token` and `call_timeout_s` apply when
you press **Start** again.

## Security

- The bridge listens on `127.0.0.1` only and every connection must present the token.
- Only `run_apex_python` changes the model. Every script is saved to
  `output\scripts\` (`query_...` or `script_...`). `query_apex_python`'s read-only check is a guard against mistakes,
  not a sandbox: Python in Apex can do anything Apex can.
- Logs are written to `output\bridge\`.

## Development

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python tests\test_units.py
.venv\Scripts\python tests\test_bridge.py
.venv\Scripts\python tests\test_scene.py
.venv\Scripts\python tests\test_script_checks.py
.venv\Scripts\python tests\test_describe.py
.venv\Scripts\python tests\test_end_to_end.py
```

The end-to-end test runs the full chain (client → server → bridge → worker) against a fake Apex
and fails if any tool iterates elements. `docs/apex-runtime-facts.md` records the live Apex 2024.1
measurements the server's design relies on; where Apex differs from its API reference is recorded in the
skill (`skills/apex-scripting/references/runtime-2024.1.md` in nastran-fea-skills). Worker edits are picked up
on the next call without restarting Apex; `server.py` edits need an MCP client restart.

## License

[MIT](LICENSE)

## Disclaimer

This is an independent, personal project. It is **not affiliated with, endorsed by, or supported
by MSC Software Corporation or Hexagon AB**. MSC Apex and MSC Nastran are trademarks of their
respective owners and are used here only to describe compatibility. You need your own licensed
copy of MSC Apex. Use at your own risk, and always review changes an AI assistant makes to your
model.
