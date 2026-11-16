# Apex 2024.1 runtime facts the server relies on

Measured live in MSC Apex 2024.1 (build CLR:011290, script units mm-kg-s-N-K, Python 3.11.6) through the
MCP bridge, on a 3-part assembly, 2026-11-13. Re-check them on a new Apex release.

Where Apex differs from its API reference (names in other modules or missing, positional arguments,
assignment, enum values, expensive properties, undo) is scripting knowledge and lives in the skill:
`skills/apex-scripting/references/runtime-2024.1.md` in
[nastran-fea-skills](https://github.com/rehman-saif/nastran-fea-skills). This file keeps the measurements
behind the server's own design: the script checks, `apex_describe` and the effects receipt.

## Signatures and errors (what the script tools rely on)

- **Unknown keywords fail at once, before the call runs:** `TypeError: getEntities() got an unexpected keyword
  argument 'paths'` (Python wrappers) or `'nme' is an invalid keyword argument for Part_update()` (SWIG). An
  unknown name raises `AttributeError` at its line. So a wrong call costs one round trip, and the worker adds the
  failing call's signature to the error.
- **Runtime signatures:** of the 1,297 functions found in `apex` and its modules (a re-exported function counts
  once per module), 827 have a real Python signature (`inspect.signature` gives the parameter names and defaults)
  and 470 are `(*args, **kwargs)` SWIG wrappers. The failing-call report and `apex_describe` show the first kind
  and say so for the second.
- **Enum values read back are ints** (`entityType` 11 for a part, `getVisibility()` 1, a scenario's
  `executionStatus` 3), so the worker maps them to names through the enum class (`apex.Visibility`,
  `apex.studies.ExecutionStatus`, `apex.studies.NastranSolutionType`).
- Modules report runtime names (`apex.mesh.__name__ == "leaf.mesh"`; entity classes have `__module__ == "glef"`),
  and the `apex` namespace re-exports `os`, `sys`, `site` and the plugin packages: `apex_describe` maps runtime
  module names back to `apex.*` and leaves the foreign ones out.

### Why names and arguments are not checked before a script runs

Versions 1.2 to 1.6 checked every `apex` name, enum member and keyword of a script against an index of the running
Apex's API (34 modules, 1,257 functions, 3,092 method names; 0.9 s and 253 kB per session). Run over the 316 saved
scripts with a recorded outcome (2026-11-14): of the 39 that failed it would have refused 4 (two of them written
to test it), and it refused 2 of the 277 that ran. Apex reports the same mistakes itself, at the call. The check
and the index were removed in 2.0; the loop guard below stays because that mistake does not raise, it exhausts
Apex's memory.

## Read or change (what query_apex_python relies on)

- Apex names carry their effect in the verb. Of the 1,257 module functions, 482 start with `get` and 435 with
  `create`; the rest are `split`, `display`, `set`, `assign`, `enable`/`disable`, `measure`, `find`, `delete` ...
  Of the 3,026 documented method names, 1,875 are `get...`, 459 `set...`, 117 `as...`.
- Read verbs: `get` (also `getuTangent`, `getisLocked`), `is`, `has`, `as`, `evaluate`, `measure`, `find`,
  `identify`, `check`, `compare`, `analyse`, `currentModel`, the `...Collection()` constructors. Exceptions:
  `geometry.findFixSmallSurfaceAndCurveFeatures` and `identifyFixSmallFeatures` repair geometry, unless called
  with `autoFix=False` (both take it; `Q.geometry_check` calls the first that way, so it only detects).
- Names that change something without a create/set/delete verb: `display.render`, `captureImage`,
  `beginUndoIndent`/`endUndoIndent`, `undo`/`redo`, `showAll`/`hideAll`/`showUI`/`hideUI`, camera `viewFromX`,
  `viewIsometric_1`, `rotate`, `zoom`, `pan`, `dolly`, `roll`, `pitch`, `yaw`, `reset`.
- `apex.setScriptUnitSystem` changes only the units scripts use; 11 saved scripts start with it, so a query
  allows it.
- The read-only rule is by these names only. It needs no index of Apex's API: a call of `apex.<...>.<name>` is a
  class or enum when the name is capitalised, otherwise a function judged by its verb.
- In the 312 scripts saved so far, the most common changes were `beginUndoIndent` (49), `deleteEntities` (32),
  `display.render` (25), `exportFEModel` (23), `update` (20) and camera setters (`setFocalPoint` 19,
  `setParallelScale` 18, `setPosition` 17).
- The bridge serves one client at a time: a second MCP server gets "Cannot reach the MCP Bridge".

## Introspection (what apex_describe relies on)

- No docstrings anywhere, so describe returns names, signatures and values only. Runtime signatures carry C++
  types (`std::vector< std::string,... > const &`, `ObjectSPtr< glef::EntityCollection >`), readable after
  simplification (`[str]`, `EntityCollection`). Methods name their self parameter `target`.
- The reference's method index often says only "inherited by 404 classes", so a class's methods are listed
  through its runtime MRO.
- Property reads that can be expensive: `elements`, `nodes`, `...Entities`, `elementIDs`, `nodeCoordinates`,
  `elementConnectivities` (whole-model collections or arrays) are never read by describe. The 33 `IOrientation`
  properties (`orientation`, `alpha`, `euler123`, `xAxis` ...) are computed on every read, ~0.25 s each: 8.4 s for
  one solid, against 0.65 s for its other 57 properties. Some properties raise on read
  (`activeRep`: "only Adams Product support activating").

## Undo

Undo history is not kept during MCP use (user decision, 2026-11-13), so the scene tools do not group their
changes. The undo measurements are in the skill's runtime-2024.1.md.

## Snapshot cost (for an effects receipt)

On 3 parts / 4 assemblies, mean of 10, for what are now the sections of `model_overview`: parts 13.6 ms
(7.9 ms without counts), connections 22.5 ms, loads and constraints 1.5 ms, catalog 2.0 ms, scenarios 2.9 ms; pathNames plus solid/surface/mesh counts ~1.4 ms per assembly or part.
Extrapolated (not measured): ~1.5 s per snapshot at 1000 parts, before mesh element arrays.
**Still to measure on a large meshed model.**

The receipt as built (`worker/effects.py`, all sections): 37 ms per snapshot unmeshed; with Part 4 meshed
(7,501 tets, 1,890 nodes) 0.10-0.11 s for the before and after snapshots together. Meshing Part 4 was reported as
`meshes [0,1], elements [0,7501], nodes [0,1890]`, the deletion as the reverse.

## Viewport and capture (from the screenshot work)

- `display.captureImage` grabs **screen pixels** of Apex's window: covering windows end up in the image;
  with `hideUI()` it returns a path and writes a **0-byte file** without raising.
- `CaptureRegionType.Viewport` = 3D area including floating panels (model browser, mechanical-system bar,
  scale bar, triad); `ViewCollection` = the whole Apex window.
- Win32 from the Apex process works: `EnumWindows` finds the main window, `WindowFromPoint` detects
  covering windows, a TOPMOST/NOTOPMOST `SetWindowPos` toggle raises Apex without taking focus.
- `getExternalVTKWidget()` returns `None` (no offscreen rendering).
- Visibility is tri-state: `Hidden 0`, `Visible 1`, `NotSet -1`. `show()`/`hide()` on a container cascade
  to all children. Changing one child explicitly turns its container `NotSet`, and it stays so.
  `session.hideAll()` sets flags on everything (avoid it when the state must be restored).
- `DisplayRenderStyle`: Wireframe 0, HiddenLines 1, Filled 2, FilledWithEdges 3, Shaded 4,
  ShadedWithEdges 5, NoRender -1, Undefined -2; parts report `-5` when none is set.
- Camera: `getDirectionOfProjection()` points **from the focal point to the camera** (`viewFromZ()` = view
  from +Z). `viewFrom*`/`viewIsometric_*` with `reset=True` fit and centre the visible scene.
  `reset()` also changes the direction. Camera coordinates and parallel scale are **not model units**
  (focal points around 1e5-1e6); rotating about the focal point, `setParallelScale` and `pan()` move
  the view unpredictably. `zoom(factor)` is reliable. A camera is restored exactly by
  `setPosition`, `setFocalPoint`, `setViewUp`, `setParallelScale` in that order.
- `setDirectionOfProjection` keeps the position and moves the focal point.
