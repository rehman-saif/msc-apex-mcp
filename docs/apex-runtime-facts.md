# Apex 2024.1 runtime facts

Measured live in MSC Apex 2024.1 (build CLR:011290, script units mm-kg-s-N-K) through the MCP
bridge, on a 3-part unmeshed assembly, 2026-11-13. These are facts the API reference
(Iberian Lynx FP2 docs, `nastran-fea-skills`) gets wrong or does not state. Re-check them on a
new Apex release before relying on them.

## API reference vs runtime

| Item | Documented | Present at runtime | Notes |
|---|---|---|---|
| Module functions | 989 | 989 | none missing |
| Classes | 733 | 728 | `DataTable2Col`/`3Col` are in `apex.catalog`, `PointSensor` in `apex.instrument`; `LocationInternal`, `studies.ModalFrequencyStep` absent |
| Enums | 237 | 229 | `ConstraintAxis` is `apex.session.ConstraintAxis`; absent: `attribute.ExtendedKeyword`, `KeywordMaterialPrimary`, `KeywordMaterialSecondary`, `MaterialModelType`, `MaterialTypeDomain`, `geometry.GeometryFaultType`, `GeometryFeatureType` |
| Enum members (single-line entries) | all | all | none missing |
| Class methods | 5104 parsed | 5104 - 236 | 220 of the 236 are generator artifacts (constructors listed as methods, `delete_RENAMED_AFTER_SWIG`); 16 are really absent (below) |

Really absent methods: `Entity.asEvent`, `asLoadCase`, `asStep`; `gendes.NonDesignRegion.getLocationInternal`;
`studies.FrequencyResponseSimulationSettings.update`; `studies.MultiBodyTransientStep.getEndTime`,
`getOutputFrequency`, `getOutputStepSize`, `getOutputSteps`; `studies.Scenario.createDynamicStep`,
`createStaticStep`, `getAcfCommands`, `getDynamicStep`, `getStaticStep`; `studies.Study.createScenarioMBDScripted`,
`createScenarioMBDStandard` (multibody / step features).

Undocumented but present (no docstrings, signatures unknown):
`apex.display.rotateView`, `panView`, `zoomAtPoint`, `zoomInView`, `zoomOutView`, `checkRotatingMode`;
`apex.geometry.createBoxByCoordinateSystem`, `createCylinderByCoordinateSystem`,
`createEllipsoidByCoordinateSystem`, `compareModel`; `apex.runPythonScript`, `runPythonFunction`,
`getShortPath`. The rest of the 189 undocumented callables are SWIG containers (`IntList`, `StringToStringMap`, ...).

## Calling conventions and signatures (what preflight relies on)

Measured on the same Apex (Python 3.11.6):

- **Positional arguments work.** `apex.getEntities(["a/b"])`, `apex.mesh.getCurveMeshes([{...}])`,
  `apex.deleteEntities(coll)` and `coll.append(p)` behave like the keyword forms. The skill's
  "Apex accepts no positional arguments" is wrong; keywords are still the better habit.
- **Unknown keywords fail at once, before the call runs:** `TypeError: getEntities() got an unexpected keyword
  argument 'paths'` (Python wrappers) or `'nme' is an invalid keyword argument for Part_update()` (SWIG).
- **Plain property assignment raises** (`AttributeError: property 'name' of 'Part' object has no setter`);
  it does not silently do nothing. `len(collection)` works as well as `.len()`.
- **Runtime signatures:** of the 1,297 functions found in `apex` and its modules (a re-exported function counts once per module), 827 have a real Python signature
  (`inspect.signature` gives the parameter names and defaults) and 470 are `(*args, **kwargs)` SWIG wrappers.
- **The documented parameter names match the runtime:** 596 of the 599 documented functions with a real signature
  have exactly the documented names (the other 3: two empty parameter lists, one with the same names in another
  order). For the SWIG wrappers, Apex's "takes at most N keyword arguments" equals the documented parameter count
  for all 314 checked. So an unknown keyword against the documented list is treated as an error.
- **Enum members the reference misses:** Apex enums are plain classes with int attributes (plus `thisown`), and
  some have members the reference omits, e.g. `SurfaceMeshElementShape.Undefined`. Only the live index can say a
  member is missing.
- Modules report runtime names: `apex.mesh.__name__ == "leaf.mesh"`, `apex.construct` is `glef.construct`,
  `apex.setting` is `clef.setting`; entity classes such as `Part` have `__module__ == "glef"`. The `apex`
  namespace also re-exports `os`, `sys`, `site` and the plugin packages.
- The live index (`introspect.api_index`: 34 modules, 1,257 functions, 576 enum-like classes) takes 0.18 s and
  is 198 kB of JSON. With the method names of every class (3,092, for query_apex_python's read-only check; 106 of
  them lower case, mostly SWIG container methods such as `push_back`, `erase`, `iteritems`) it takes 0.92 s and is
  253 kB, built once per server session.

## Read or change (what query_apex_python relies on)

- Apex names carry their effect in the verb. Of the 1,257 module functions, 482 start with `get` and 435 with
  `create`; the rest are `split`, `display`, `set`, `assign`, `enable`/`disable`, `measure`, `find`, `delete` ...
  Of the 3,026 documented method names, 1,875 are `get...`, 459 `set...`, 117 `as...`.
- Read verbs: `get` (also `getuTangent`, `getisLocked`), `is`, `has`, `as`, `evaluate`, `measure`, `find`,
  `identify`, `check`, `compare`, `analyse`, `currentModel`, the `...Collection()` constructors. Exceptions:
  `geometry.findFixSmallSurfaceAndCurveFeatures` and `identifyFixSmallFeatures` repair geometry, unless called
  with `autoFix=False` (both take it; `geometry_check` calls the first that way, so it only detects).
- Names that change something without a create/set/delete verb: `display.render`, `captureImage`,
  `beginUndoIndent`/`endUndoIndent`, `undo`/`redo`, `showAll`/`hideAll`/`showUI`/`hideUI`, camera `viewFromX`,
  `viewIsometric_1`, `rotate`, `zoom`, `pan`, `dolly`, `roll`, `pitch`, `yaw`, `reset`.
- `apex.setScriptUnitSystem` changes only the units scripts use; 11 saved scripts start with it, so a query
  allows it.
- In the 312 scripts saved so far, the most common changes were `beginUndoIndent` (49), `deleteEntities` (32),
  `display.render` (25), `exportFEModel` (23), `update` (20) and camera setters (`setFocalPoint` 19,
  `setParallelScale` 18, `setPosition` 17).
- The bridge serves one client at a time: a second MCP server gets "Cannot reach the MCP Bridge".

## Introspection (what apex_describe relies on)

- **No docstrings anywhere:** modules, functions, classes, methods and properties all have empty `__doc__`.
  Descriptions can only come from the API reference.
- Runtime signatures carry C++ types (`std::vector< std::string,... > const &`, `ObjectSPtr< glef::EntityCollection >`),
  readable after simplification (`[str]`, `EntityCollection`). Methods name their self parameter `target`.
- Entity classes are SWIG classes in `glef`: `Part` has 57 `property` objects and 190 methods; its MRO is
  `Part, Entity, IPhysical, ILocation, IDisplayable, IUserAttributes, IName, IPath, IActivatable`. The reference's
  method index often says only "inherited by 404 classes", so inherited methods are matched through the runtime MRO.
- Property reads that can be expensive: `elements`, `nodes`, `...Entities`, `elementIDs`, `nodeCoordinates`,
  `elementConnectivities` (whole-model collections or arrays) are never read by describe. The 33 `IOrientation`
  properties (`orientation`, `alpha`, `euler123`, `xAxis` ...) are computed on every read, ~0.25 s each: 8.4 s for
  one solid, against 0.65 s for its other 57 properties. Some properties raise on read
  (`activeRep`: "only Adams Product support activating").
- `apex.mesh` also defines classes for ~90 enums the reference documents under `apex.attribute` (separate
  classes with the same names, `__module__ == "leaf.mesh"`).
- `Part.colorRGB` is (112, 255, 184) on every part while `color` / `getColor()` give the displayed colour; it is not
  the display colour.

## Undo

- `apex.setting.getUndoRedoStackSize()` = **50**.
- `apex.undo()` reverts, one command per step: part create, rename (`update(name=)`), `deleteEntities`,
  `hide()`/`show()`, `update(color=)`. All verified.
- **Works across bridge calls.** A bridge call is **not** grouped: two creates in one call need two undos.
- `beginUndoIndent()` ... `endUndoIndent(description=)` makes everything in between **one** step
  (verified with 2 creates and with 57 visibility toggles). Close it in `finally`.
- **Visibility and colour changes are undo entries.** 56 ungrouped toggles pushed an older entry off the
  50-entry stack. Grouping would keep the user's history, but undo history is not needed during MCP use (user
  decision, 2026-11-13), so the scene tools do not group their changes.
- Safe probing rule: after each `undo()` check the expected effect; if absent, `redo()` at once.

## Snapshot cost (for an effects receipt)

On 3 parts / 4 assemblies, mean of 10: `list_tree` 13.6 ms (7.9 ms without counts),
`list_connections` 22.5 ms, `list_loads_constraints` 1.5 ms, `list_catalog` 2.0 ms,
`list_scenarios` 2.9 ms; pathNames plus solid/surface/mesh counts ~1.4 ms per assembly or part.
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
