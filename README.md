# Apex MCP

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude, Codex, Cursor, Gemini CLI, ...) work inside **your own running MSC Apex session** for FEA pre-processing: inspect the model, check CAD, build meshes, loads and scenarios, drive the viewport and run Apex Python.

## Features

- **Read the model:** tree, part and mesh summaries, faces, connections, loads, constraints, scenarios.
- **Check geometry:** slivers, small edges and faces, gaps and cracks, reported without repairs.
- **Build models:** materials, solid properties, meshing, glue, RBE2, beams, constraints, nodal loads and static scenarios.
- **Check decks:** exports a BDF and returns a compact summary with lint warnings.
- **Drive the viewport:** isolate or hide parts, colour them, take several views in one labelled screenshot, save named views.
- **Run Apex Python:** each script is checked against the running Apex's API before it runs, and the result says
  what it changed in the model. A read-only script tool refuses anything that would change the model, so reads need
  no confirmation. An API lookup and scripting guide help the assistant write correct calls.

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
- For Apex Python, the server serves the `apex-scripting` skill: exact API signatures
  (`apex_api_lookup`) and scripting rules (`apex_scripting_guide`). `apex_describe` reads what the
  running Apex really has. The first script of a session waits until one of them has been
  consulted.

## Tools

| Group | Tools |
|---|---|
| Session | `apex_status`, `reconnect` |
| Model (read-only) | `list_tree`, `part_summary`, `geometry_check`, `get_entity`, `find_entities`, `list_faces`, `list_connections`, `list_loads_constraints`, `list_catalog`, `list_scenarios` |
| Model building | `import_fe_model`, `create_material`, `assign_solid_property`, `mesh_solid`, `glue`, `rbe2`, `beam`, `constrain`, `nodal_load`, `static_scenario` |
| Deck | `export_bdf` |
| Viewport | `viewport`, `screenshot` (see below) |
| Apex API help | `apex_api_lookup`, `apex_scripting_guide`, `apex_describe` |
| Apex Python | `query_apex_python` (read-only), `run_apex_python` (changes) |

Builder tools are idempotent: calling one again with the same name replaces what it made before.

Every tool carries MCP annotations: the read tools (session status, model summaries, API help, `apex_describe`,
`query_apex_python`) are marked read-only, `viewport`, `screenshot`, `export_bdf` and `reconnect` as
non-destructive, and the builders, `import_fe_model` and `run_apex_python` as destructive. To stop being asked
before every read, allow the read tools in your client, e.g. in Claude Code's `settings.json`:

```json
{"permissions": {"allow": ["mcp__apex__list_tree", "mcp__apex__part_summary", "mcp__apex__apex_describe",
                           "mcp__apex__query_apex_python"]}}
```

### Viewport and screenshots

`viewport` and `screenshot` take the same **scene** description (everything optional):

```jsonc
{
  "show_only": ["base/Assembly/Housing"],   // or show / hide / show_all / hide_all: parts, assemblies, solids, surfaces
  "style": {"color_by_part": true,          // parts often share one colour; or "color": {"<path>": [r,g,b]}
            "render_style": "ShadedWithEdges", "transparency": {"<path>": 40},
            "mesh_edges": false, "loads_bc_markers": true, "connection_markers": true},
  "highlight": [{"paths": ["..."]}, {"part_path": "...", "ids": [1, 2, 3], "kind": "Element"}],
  "labels": [{"text": "bolt", "at": [0, 0, 10]}],
  "cut": {"location": [0, 0, 5]}
}
```

- **`viewport(scene, view, save_view, delete_view, list_views)`** applies the scene and leaves it applied. `view` is a
  preset, a saved view or `{preset|saved, zoom}`.
- **`screenshot(scene, views, layout, crop, margin, max_width, restore)`** applies the scene, takes up to 9 views, and
  puts your scene and camera back. `layout: "sheet"` (default) returns one labelled grid, `"separate"` one image each.
- **Views:** presets `iso1`-`iso4`, `x`, `-x`, `y`, `-y`, `z`, `-z` and `right` (+X), `left` (-X), `back` (+Y), `front`
  (-Y), `top` (+Z), `bottom` (-Z), named for the side the camera is on. Apex fits a preset to what is visible, so
  `show_only` a part to frame it. For any other angle set the view up in Apex and `save_view` it.

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
  the entities inside them). **Not restorable** because Apex cannot read them back: marker/edge toggles, highlights and
  cut views. A screenshot lists what it used under `not_restored`.
- There is no free rotation, pan or camera-position input: the camera's coordinates and parallel scale are not model
  units in Apex, and rotating about the focal point throws a part out of frame. Presets, `zoom` and saved views are
  the supported, reliable ways to move the camera.

### Running Apex Python

Two tools run a script on Apex's script thread, with the same namespace (variables persist between them):

- **`query_apex_python(code, force, show_log_path)`** for reading: counts, measurements, ids, property checks. It is
  refused before it runs if it would change anything (below), so it can be allowed without a prompt.
- **`run_apex_python(code, force, effects, attach, show_log_path)`** for changes to the model or the scene.

Around both:

1. **Preflight** (in the server, nothing runs). The script is read with Python's `ast` and its `apex` names are
   checked against the running Apex's own API (collected once per session by the bridge) and the API reference.
   It is **refused** only for things that are sure to fail or to hurt Apex:
   - a syntax error;
   - an `apex` function, class, enum or enum member that this Apex does not have (with "did you mean" and, for
     names the reference puts in the wrong module, where they really are, e.g. `apex.session.ConstraintAxis`);
   - a keyword argument the function does not take, a missing required argument, too many positional ones;
   - a loop over `.elements` / `.nodes` (walking element objects exhausted Apex's memory on a 90k-element model).

   Anything less certain is a warning shown with the output. A name used inside `try`/`except` or under
   `if hasattr(...)` only warns. `force=True` runs a refused script anyway. Checked on 279 saved scripts: none of the
   243 that ran was refused, and 2 of the failing ones would have been caught before running.
2. **Effects.** A snapshot before and after the script (parts, assemblies, solid / surface / curve / mesh / element /
   node counts per part, loads, constraints, connections, materials, properties, scenarios) gives a line like
   `effects={"changed":{"base/Bracket/Part 4":{"meshes":[0,1],"elements":[0,7501],"nodes":[0,1890]}}}`, or
   `effects: none`. A script that fails halfway reports what it changed before the error. Values, assignments,
   coordinates and visibility are not tracked; a rename shows as a deletion plus a creation. Both snapshots took
   0.1 s on a small meshed model; `effects_budget_s` caps them on large ones (an unfinished section is reported as
   not tracked rather than guessed). `effects=False` skips them.
3. **Errors.** When the script raises, the output names the failing line and adds the API-reference signature of
   the `apex` call that failed on it, resolved in the script's own frame (so `part.update(...)` is reported as
   `Part.update`).

**What makes a query read-only.** Apex names say what they do: functions and methods named `get...`, `is...`,
`as...`, `evaluate...`, `measure...`, `find...` read (`...Fix...` repairs geometry unless called with
`autoFix=False`), the rest
(`create...`, `set...`, `delete...`, `assign...`, `update`, `hide`, `display.render`, `undo` ...) change the model,
the scene or the session. `query_apex_python` refuses, and `force=True` cannot override:

- an `apex` function that is not a read, also when it is only referenced (`f = apex.deleteEntities`);
- a method with a changing verb, or an Apex method (the live index lists all 3,092) that is not a read;
  `x.update(...)` unless `x` is a dict the script builds itself (`{}`, `dict()`);
- writing a file (`open(.., 'w')`, `np.save`, `Path.write_text` ...), `os.remove` / `os.system` and the like,
  imports of `subprocess`, `shutil`, `socket`, `ctypes` ..., and `exec` / `eval` / `setattr`.

Adding to a list or an `EntityCollection`, numpy, `json`, `math`, reading files and
`apex.setScriptUnitSystem` are fine. This guards against mistakes; it is not a sandbox. So a query is always
snapshotted (even with `script_effects` off), and if it changed the model anyway the result starts with
`WARNING: this query CHANGED THE MODEL` and the effects. Of 312 saved scripts, 155 pass the read-only check; the
others create, delete, mesh, move the camera, render, group undo steps or write files.

### Describing the live API

The API reference is generated documentation and Apex 2024.1 differs from it in places (classes in other modules,
enum members it leaves out, undocumented functions), while Apex's own objects carry no docstrings.
`apex_describe(target, filter, values, limit)` reads the running Apex and adds the reference's signatures and
descriptions:

| `target` | Returns |
|---|---|
| `apex.mesh` | functions, classes, enums; `*` = not in the reference, `^` = documented under another module |
| `apex.mesh.createSolidMesh`, `apex.Part.update` | the runtime signature (with types, where Apex exposes one) next to the documented one |
| `apex.mesh.SurfaceMeshElementShape` | members with their values, marking ones the reference lacks |
| `apex.Part` | properties and methods (inherited ones too, via the runtime class hierarchy) |
| an entity pathName, e.g. `base/Assembly/Part 4` | its class and current property values, e.g. bounding box, colour, counts of solids and meshes |
| `$part`, `$res.meshes` | a variable left by `run_apex_python` |

Properties that can hold every element or node (`elements`, `nodes`, `...Entities`, `...IDs`) are never read.

## Configuration

Settings live in `config.json` (created by `setup.bat` from `config.example.json`, not committed).

| Key | Meaning |
|---|---|
| `bridge_port` / `bridge_token` | Where the bridge listens and its shared secret |
| `script_unit_system` | e.g. `mm-t-s-N-K`; empty leaves Apex's units alone |
| `output_dir` | Screenshots, saved views, saved scripts, exported decks and logs |
| `screenshot_max_width` | Width cap for screenshots (default 1400) |
| `screenshot_bring_to_front` | Raise Apex's window when something covers it before a capture (default `true`) |
| `call_timeout_s` | Maximum wait for one Apex call (default 300) |
| `query_time_budget_s` / `max_items` / `max_response_kb` | Limits that keep queries small and fast |
| `require_skill_lookup` | Require an API lookup before the first raw script (default `true`) |
| `script_preflight` | Check `run_apex_python` scripts before they run (default `true`; `query_apex_python` is always checked) |
| `script_effects` / `effects_budget_s` / `effects_mesh_counts` | Effects receipt on or off (default `true`), its time cap per snapshot (default 5 s), and whether it counts mesh elements and nodes (default `true`) |

Most changes apply on the next call; `bridge_port`, `bridge_token` and `call_timeout_s` apply when
you press **Start** again.

## Security

- The bridge listens on `127.0.0.1` only and every connection must present the token.
- Only the model-building tools, `import_fe_model` and `run_apex_python` change the model. Every script is saved to
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
.venv\Scripts\python tests\test_end_to_end.py
```

The end-to-end test runs the full chain (client → server → bridge → worker) against a fake Apex
and fails if any tool iterates elements. `docs/apex-runtime-facts.md` records what was measured in a live
Apex 2024.1 where it differs from the API reference; `apex_mcp/runtime_overrides.json` is the part of it that
preflight uses. Worker edits are picked up on the next call without
restarting Apex; `server.py` edits need an MCP client restart.

## License

[MIT](LICENSE)

## Disclaimer

This is an independent, personal project. It is **not affiliated with, endorsed by, or supported
by MSC Software Corporation or Hexagon AB**. MSC Apex and MSC Nastran are trademarks of their
respective owners and are used here only to describe compatibility. You need your own licensed
copy of MSC Apex. Use at your own risk, and always review changes an AI assistant makes to your
model.
