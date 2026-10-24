# Apex MCP bridge

Lets Claude work in **your own open MSC Apex 2024.1 session** for pre-processing:
inspecting the model, checking CAD, driving the viewport and running Apex Python.
Generic: nothing here knows about a particular model. Post-processing and .h5
results tools were removed (October 2026).

```
Claude ──stdio──> apex_mcp\server.py   (.venv: Python 3.14 + mcp)
                     │  JSON over 127.0.0.1:47115 + token (config.json), one call at a time
                     ▼
Apex ── palette "Claude" ── tool "Claude Bridge"   [Start] [Stop] [Self-test]
          listener thread (UI side) ──apex_sdk.runScriptFunction──> Apex script thread
                                            claude_bridge_worker.handle
                                            -> apex_mcp\worker\apex_worker.py (+ queries.py)
```

## Install (once)

Needs Windows, MSC Apex 2024.1, Python 3.10+ and git (without git the skills are downloaded as a zip).

1. Run **`setup.bat`**. It creates `.venv`, writes `config.json` with a fresh `bridge_token`,
   clones the [nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills) repo into
   `vendor\` (see [Apex API grounding](#apex-api-grounding)) and writes MCP client snippets with
   this folder's paths. If Claude Code is installed it offers to install the FEA skills plugin
   and register this server too (`setup.bat /y` does that without asking).
2. **Register the palette in Apex:** Options → Application Settings → Custom Tools →
   add `<this folder>\apex_palette`. Restart Apex if the **Claude** palette doesn't appear.
3. Open your model, then **Claude → Claude Bridge → Self-test**.
4. Connect your MCP client:

| Client | Where |
|---|---|
| Claude Code | done by `setup.bat` (or `claude mcp add --scope user apex -- <python> <server.py>`) |
| Claude Desktop | merge `mcp_config.snippet.json` into `%APPDATA%\Claude\claude_desktop_config.json`, restart |
| Cursor | merge `mcp_config.snippet.json` into `%USERPROFILE%\.cursor\mcp.json` |
| Gemini CLI | merge `mcp_config.snippet.json` into `%USERPROFILE%\.gemini\settings.json` |
| Codex CLI | append `codex_config.snippet.toml` to `%USERPROFILE%\.codex\config.toml` |

Re-run `setup.bat` to update the skills.

## Daily use

Open Apex and your model, open **Claude Bridge** and press **Start**. Press **Stop** when done.
Edits to `apex_worker.py`, `builders.py`, `queries.py` or `serialize.py` are picked up on the
next call without restarting Apex. The bridge calls `apex.disableShowOutput()` on every call, so Apex's
script-output dialog no longer opens and blocks the next call. Edits to `server.py` or
`skills.py` need the MCP client restarted.

Apex must load the bridge from **this** folder. If Apex's Custom Tools entry points at another
copy of Apex_MCP, the server and the bridge drift apart and edits never reach Apex.
`apex_status` reports both folders (`deployment.server_root` / `bridge_root`), compares the
worker and palette files by hash, and warns when they differ.

## Memory safety (why the query tools were rewritten)

Every tool runs on Apex's single script thread, inside Apex's memory. The old
`get_entity` walked up to 20,000 element objects and read material/property on each. On
the 90k-element composite part that exhausted Apex's memory and crashed it, while further
calls queued up behind it. Now:

- No tool iterates `part.elements`, `part.nodes`, `mesh.elements` or `mesh.nodes`. Mesh
  statistics come from the bulk arrays (`getElementIDs`, `getNodeIDs`,
  `getNodeCoordinates`). Property and material come from a few **sampled** elements per mesh.
- Every entity loop runs under a time budget (`query_time_budget_s`) and an item cap
  (`max_items`), and reports `truncated` / `budget_exceeded` instead of running on.
- Every response is size-checked (`max_response_kb`). An oversized response returns an error
  plus a preview, not megabytes of JSON.
- The server sends one call at a time. A new call while one is still running fails at once
  with "Apex is busy", instead of queueing.
- `run_apex_python` has the same rules in its description, and the `Q` helper module is
  preloaded (`Q.part_summary`, `Q.mesh_summary`, `Q.sample_elements`, `Q.topology_counts`,
  `Q.solid_summary`, `Q.surface_summary`, `Q.bbox`, `Q.Budget`, `Q.faces_by_id`,
  `Q.nodes_on_face`, `Q.nodes_where`, `Q.nodes_by_id`, `Q.node_near`, `Q.rle`). A script that
  binds `Q` or `S` itself keeps its value; `import queries as Q` brings the helpers back.

## Tools (33)

| Group | Tools |
|---|---|
| Session | `apex_status`, `reconnect` |
| Model (read-only) | `list_tree`, `part_summary`, `geometry_check`, `get_entity`, `find_entities`, `list_faces`, `list_connections`, `list_loads_constraints`, `list_catalog`, `list_scenarios` |
| Model (changes it) | `import_fe_model` |
| Builders (change it) | `create_material`, `assign_solid_property`, `mesh_solid`, `glue`, `rbe2`, `beam`, `constrain`, `nodal_load`, `static_scenario` |
| Deck | `export_bdf` |
| Viewport | `visibility`, `set_view`, `display_options`, `highlight`, `annotate`, `cut_view`, `screenshot` |
| Apex API (local, read-only) | `apex_api_lookup`, `apex_scripting_guide` |
| Raw | `run_apex_python` |

- `part_summary`: per mesh, element counts by topology, node count, associated geometry,
  and the 2D/3D property, material and thickness of N sampled elements. Per solid: volume,
  face and edge counts, and face-area range. Per surface: area, free-edge count and manifold flag.
- `geometry_check`: Apex `findGeometryFaults` (Sliver, SmallEdge, SmallFace, Overhang, Gap,
  Crack, Spike) and `findFixSmallSurfaceAndCurveFeatures` with `autoFix=False`, plus
  edge-length and face-area ranges per body. It repairs nothing.
- `list_faces`: one compact row per face, `[id, kind, area, centroid, dir, r]`, where kind is
  plane, cyl or curved, dir is the normal or axis, and r is the cylinder radius. Filters are
  `kind`, `near`+`radius` and `normal`. The ids go into builder targets.
- Builders wrap the Apex calls proven in real sessions. Each one returns ids and counts only,
  and each is idempotent: a repeat call with the same name (for `rbe2`, the same reference node)
  replaces what the earlier call made. `mesh_solid` deletes the old mesh on those solids first.
  `constrain` and `nodal_load` keep their SPC/LOAD set id when re-created, so scenarios still
  point at them. `beam` returns the mesh node at each input point, ready to use as an `rbe2` ref.
  `static_scenario` reads LOAD/SPC/BCONTACT back after setting them. Targets are a pathName,
  `{"path": part, "faces": [ids]}`, `{"path": part, "nodes": [ids]}`, or a list of these.
- `export_bdf`: writes the scenario's deck, then reads it in the MCP server (not in Apex) and
  returns only SOL, case control, card counts and lint warnings. The lint covers contact cards
  without a matching BCONTACT, subcases without LOAD/SPC or pointing at missing sets, RBE2
  dependent nodes that are SPC'd or in two RBE2s, missing property/material/GRID references,
  and duplicate ids. `python apex_mcp\bdf_summary.py deck.bdf` runs the same check on any deck.
- `run_apex_python` returns plain text: stdout, then `result=<compact JSON>`, then on failure the
  script's own traceback frames and where the script was saved.

## Apex API grounding

Most work goes through `run_apex_python`, and agents writing Apex Python from memory get it
wrong: positional arguments, assignment instead of `update()`, invented enum members. So the
server serves the `apex-scripting` skill from `vendor\nastran-fea-skills` to every MCP client:

- `apex_api_lookup(query, kind)` returns exact Iberian Lynx FP2 signatures with per-argument
  docs, class properties and methods, and enum members (989 functions, 733 classes, 237 enums).
- `apex_scripting_guide(topic)` returns the scripting rules: `scripting-model`, `pitfalls`,
  `patterns-and-recipes`, `module-selection`, `debugging-playbook` and the rest.
- The first `run_apex_python` of a session is refused until one of them has been called, and
  a script that raises comes back with a reminder to look the failing call up. Set
  `require_skill_lookup` to `false` to turn the gate off.

In Claude Code, `setup.bat` also installs the full `fea-structural-help` plugin, which adds
Apex GUI docs, Nastran reference, Nastran diagnostics and modelling strategy.

## Safety

- The bridge listens on 127.0.0.1 only, and every connection must present `bridge_token`.
- Only `import_fe_model`, the builder tools and `run_apex_python` can change the model. Every raw
  script is saved to `output\scripts\`.
- Logs: `output\bridge\bridge_ui.log` (listener) and `output\bridge\bridge_worker.log` (calls).

## Config (`config.json`)

| key | meaning |
|---|---|
| `bridge_port` / `bridge_token` | where the Apex tool listens, and the shared secret |
| `script_unit_system` | e.g. `mm-kg-s-N`; empty leaves Apex's script units alone |
| `output_dir` | screenshots, scripts, exported decks (`bdf\`), logs |
| `call_timeout_s` | maximum seconds the server waits for one Apex call |
| `query_time_budget_s` | wall-clock budget for entity loops inside one query (default 20) |
| `max_items` | default cap on listed items per query (default 200) |
| `max_response_kb` | largest response sent back (default 256) |
| `max_highlight_ids` | largest ID list `highlight` accepts (default 20000) |
| `skills_dir` | nastran-fea-skills checkout to use; empty means `vendor\nastran-fea-skills` |
| `require_skill_lookup` | refuse the first `run_apex_python` until the API was consulted (default true) |

`config.json` holds the bridge token and is not committed; `config.example.json` is the template.

## Developing

From `.venv`, run `python tests\test_units.py` (BDF lint, deployment check, output format) and
`python tests\test_end_to_end.py` (needs the skills in `vendor\`, or
`APEX_SKILLS_DIR`; the `screenshot` step needs Pillow, which Apex ships but the venv does not). This runs the whole chain (MCP client →
server → bridge UI code → worker) against a fake `apex`. Its `part.elements`/`mesh.elements`
raise if touched, so the test fails if any tool walks elements.

`smoke_test.py`, `run_smoke_test.bat` and `probe_attach.*` are
left over from the first `remoting` attempt and are unused.
