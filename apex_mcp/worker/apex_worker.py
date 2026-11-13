"""Apex tool handlers, run in-process on Apex's script thread.

The "MCP Bridge" palette tool calls set_inprocess() once, then handle_request(req)
for each request (via apex_sdk.runScriptFunction -> mcp_bridge_worker.handle).

Requests/responses:  {"id", "method", "params"}  ->  {"id", "ok", "result" | "error"}.

Memory safety (why this file was rewritten)
-------------------------------------------
Apex runs everything here on its single script thread, in its own memory. A handler
that walks every element of a large mesh in Python can exhaust that memory and take
Apex down. So:
  * no handler iterates part.elements / part.nodes / mesh.elements; mesh statistics
    come from the bulk ID/coordinate arrays (see queries.py) and a few sampled elements;
  * every loop over entities runs under a time Budget and an item cap;
  * every response is size-checked before it is sent (max_response_kb).
"""
import contextlib
import datetime
import hashlib
import io
import json
import linecache
import os
import sys
import time
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # the Apex_MCP folder this bridge runs from
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import builders as B  # noqa: E402
import effects as FX  # noqa: E402
import introspect as IN  # noqa: E402
import queries as Q  # noqa: E402
import scene as SC  # noqa: E402
import serialize as S  # noqa: E402

# Files whose copies must match between the MCP server and the bridge (apex_status compares them).
DEPLOYED_FILES = ["apex_mcp/worker/apex_worker.py", "apex_mcp/worker/queries.py",
                  "apex_mcp/worker/serialize.py", "apex_mcp/worker/builders.py", "apex_mcp/worker/scene.py",
                  "apex_mcp/worker/effects.py", "apex_mcp/worker/introspect.py",
                  "apex_palette/MCP/Bridge/MCP Bridge/src/mcp_bridge_worker.py",
                  "apex_palette/MCP/Bridge/MCP Bridge/src/mcp_bridge_ui.py"]


def file_hashes(root, rels=DEPLOYED_FILES):
    """{relative path: first 12 hex chars of sha256, or None if missing}."""
    out = {}
    for rel in rels:
        try:
            with open(os.path.join(root, *rel.split("/")), "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            out[rel] = None
    return out

CFG = {}
MODE = {"inprocess": False, "log_file": None}
STATE = {"attached": False, "pid": None, "ns": None, "apex": None}

DEFAULTS = {"query_time_budget_s": 20.0, "max_items": 200, "max_response_kb": 256,
            "max_highlight_ids": 20000, "effects_budget_s": 5.0, "effects_mesh_counts": True}


# ------------------------------------------------------------------ util
def cfg(key):
    return CFG.get(key, DEFAULTS.get(key))


def log(*a):
    line = " ".join([datetime.datetime.now().strftime("%H:%M:%S")] + [str(x) for x in a])
    if MODE.get("log_file"):
        try:
            with open(MODE["log_file"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        except Exception:
            pass
    print(line, file=sys.stderr, flush=True)


def set_inprocess(config=None):
    """Use the `apex` module of the Apex process this code is running in."""
    if config:
        CFG.clear()
        CFG.update(config)
        STATE["attached"] = False  # ensure_apex applies script_unit_system again
    MODE["inprocess"] = True
    MODE["log_file"] = os.path.join(out_dir("bridge"), "bridge_worker.log")


def out_dir(kind):
    d = os.path.join(CFG.get("output_dir") or os.path.join(HERE, "..", "..", "output"), kind)
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def enum(enum_cls, name, what):
    if name is None:
        return None
    if not hasattr(enum_cls, name):
        members = [m for m in dir(enum_cls) if not m.startswith("_")]
        raise ValueError("unknown %s '%s'. Valid: %s" % (what, name, ", ".join(members[:150])))
    return getattr(enum_cls, name)


def apex_bool(v):
    apex = STATE["apex"]
    cand = "ApexBoolTrue" if v else "ApexBoolFalse"
    if hasattr(apex, cand):
        return getattr(apex, cand)
    if hasattr(apex, "ApexBool") and hasattr(apex.ApexBool, cand):
        return getattr(apex.ApexBool, cand)
    return bool(v)


def limit_of(p):
    return max(1, min(int(p.get("limit") or cfg("max_items")), S.MAX_LIST))


def ensure_apex():
    if STATE["attached"]:
        return STATE["apex"]
    import apex
    STATE.update(attached=True, pid=str(os.getpid()), apex=apex)
    units = CFG.get("script_unit_system")
    if units:
        apex.setScriptUnitSystem(unitSystemName=units)
    return apex


def entities_by_path(paths):
    apex = ensure_apex()
    coll = apex.EntityCollection()
    missing = []
    for p in paths:
        try:
            got = apex.getEntities(pathNames=[p])
            if got.len() == 0:
                missing.append(p)
            else:
                coll.extend(collection=got)
        except Exception:
            missing.append(p)
    if missing:
        raise KeyError("not found: %s (use list_tree / find_entities for exact pathNames)" % missing)
    return coll


def one_entity(path):
    return entities_by_path([path])[0]


def parts_from(paths):
    """Parts named in paths; an assembly/model path expands to its parts (recursive)."""
    apex = ensure_apex()
    if not paths:
        return list(apex.currentModel().getParts(recursive=True))
    out = []
    for e in entities_by_path(paths):
        if hasattr(e, "getParts"):
            out.extend(list(e.getParts(recursive=True)))
        else:
            out.append(e)
    return out


# ----------------------------------------------------------- model tools
def m_status(p):
    out = {"worker_python": sys.version.split()[0], "attached": STATE["attached"],
           "limits": {k: cfg(k) for k in DEFAULTS},
           "config": {k: v for k, v in CFG.items() if "token" not in k},
           "deployment": {"root": ROOT, "config_path": os.path.join(ROOT, "config.json"),
                          "hashes": file_hashes(ROOT)}}
    if p.get("connect", True):
        apex = ensure_apex()
        out["attached"], out["pid"] = True, STATE["pid"]
        out["application"] = S.to_json(apex.getApplicationInfo())
        model = apex.currentModel()
        out["model"] = S.ref(model) if model is not None else None
        for k, fn in (("script_units", "getScriptUnitSystemLabel"),
                      ("application_units", "getApplicationUnitSystemLabel"),
                      ("project_folder", "getCurrentProjectFolderPath")):
            try:
                out[k] = getattr(apex, fn)()
            except Exception as exc:
                out[k] = "<error: %s>" % exc
    return out


def _part_counts(part):
    rec = S.ref(part)
    for k, getter in (("solids", "getSolids"), ("surfaces", "getSurfaces"), ("meshes", "getMeshes")):
        rec[k] = Q.clen(Q.safe(lambda g=getter: getattr(part, g)()))
    # element/node totals from the bulk ID arrays of each mesh (no element objects)
    ne = nn = 0
    for m in Q.safe(lambda: part.getMeshes()) or []:
        c, _ = Q.topology_counts(m)
        ne += sum(c.values())
        ids = Q.safe(lambda: m.getNodeIDs())
        nn += len(ids) if ids is not None else 0
    rec["elements"], rec["nodes"] = ne, nn
    rec["visibility"] = Q.enum_name(Q.safe(lambda: part.getVisibility(), "?"))
    return rec


def m_list_tree(p):
    apex = ensure_apex()
    counts = bool(p.get("counts", True))
    max_depth = int(p.get("max_depth", 10))
    b = Q.Budget(cfg("query_time_budget_s"))
    cap = limit_of(p)
    seen = {"n": 0}

    def walk(asm, depth):
        node = S.ref(asm)
        parts = []
        for pt in Q.safe(lambda: asm.getParts(recursive=False)) or []:
            seen["n"] += 1
            if seen["n"] > cap or b.over():
                node["truncated"] = True
                break
            parts.append(_part_counts(pt) if counts else S.ref(pt))
        node["parts"] = parts
        if depth < max_depth and not b.hit:
            node["assemblies"] = [walk(a, depth + 1)
                                  for a in (Q.safe(lambda: asm.getAssemblies(recursive=False)) or [])]
        return node

    tree = walk(apex.currentModel(), 0)
    if b.hit:
        tree["budget_exceeded"] = True
    return tree


def m_part_summary(p):
    """Per-mesh topology counts + sampled element attributes + per-body geometry stats."""
    ensure_apex()
    sample = max(0, min(int(p.get("sample_per_mesh", 3)), 50))
    b = Q.Budget(cfg("query_time_budget_s"))
    parts = parts_from(p.get("paths"))
    out = []
    for part in parts:
        if b.over():
            break
        out.append(Q.part_summary(part, sample=sample, geometry=bool(p.get("geometry", True)),
                                  budget_s=b.limit - b.elapsed(), max_items=limit_of(p)))
    res = {"parts": out}
    if len(out) < len(parts):
        res["budget_exceeded"] = "summarised %d of %d parts; ask for fewer with paths" % (len(out), len(parts))
        res["parts_skipped"] = len(parts) - len(out)
    return res


def m_geometry_check(p):
    """Read-only CAD health check: Apex geometry faults, small features, per-body stats."""
    apex = ensure_apex()
    b = Q.Budget(cfg("query_time_budget_s"))
    cap = limit_of(p)
    tol = float(p.get("small_feature_tolerance", 0.5))
    bodies = apex.EntityCollection()
    report = {"parts": [], "tolerance": tol}
    for part in parts_from(p.get("paths")):
        prec = {"part": S.ref(part), "solids": [], "surfaces": []}
        for s in Q.safe(lambda: part.getSolids()) or []:
            bodies.append(entity=s)
            if len(prec["solids"]) < cap and not b.over():
                prec["solids"].append(Q.solid_summary(s, b))
        for s in Q.safe(lambda: part.getSurfaces()) or []:
            bodies.append(entity=s)
            if len(prec["surfaces"]) < cap and not b.over():
                prec["surfaces"].append(Q.surface_summary(s, b))
        prec["already_meshed"] = bool(Q.clen(Q.safe(lambda: part.getMeshes())))
        report["parts"].append(prec)
    if b.hit:
        report["budget_exceeded"] = "per-body stats incomplete; check fewer parts"
    report["bodies_checked"] = bodies.len()
    if bodies.len() == 0:
        return report
    if p.get("faults", True):
        try:
            res = apex.geometry.findGeometryFaults(target=bodies)
            by_type = {}
            for t in Q.safe(lambda: list(res.faultTypes), []) or []:
                name = Q.fault_type_name(t)
                by_type[name] = by_type.get(name, 0) + 1
            # Apex 2024.1 returns the fault locations as an unwrapped SWIG vector that Python
            # cannot iterate, so only counts are reported; per-body stats above show where.
            report["geometry_faults"] = {"count": int(res.numGeometryFaults), "by_type": by_type}
        except Exception as exc:
            report["geometry_faults_error"] = str(exc)[:300]
    if p.get("small_features", True):
        try:
            res = apex.geometry.findFixSmallSurfaceAndCurveFeatures(
                target=bodies, enableGapDetection=True, enableOverhangDetection=True,
                enableSmallEdgeDetection=True, tolerance=tol, autoFix=False)
            report["small_features"] = {"count": int(res.numFeaturesIdentified),
                                        "note": "details not readable from Python in Apex 2024.1"}
        except Exception as exc:
            report["small_features_error"] = str(exc)[:300]
    report["seconds"] = b.elapsed()
    return report


def m_get_entity(p):
    """Reference, bounding box and the listed properties of one entity. No element walking."""
    ensure_apex()
    e = one_entity(p["path"])
    props = p.get("properties") or []
    rec = S.describe(e, props, depth=0) if props else S.ref(e)
    rec["bounding_box"] = Q.bbox(e)
    if hasattr(e, "getMeshes"):
        rec["hint"] = "use part_summary for mesh / property / material detail"
    return rec


def m_list_faces(p):
    """Compact face rows (id, kind, area, centroid, dir, r) per part, filtered."""
    ensure_apex()
    kind = p.get("kind")
    if kind not in (None, "plane", "cyl", "curved"):
        raise ValueError("kind must be plane, cyl or curved")
    near = p.get("near")
    if near is not None and p.get("radius") is None:
        raise ValueError("near needs radius")
    b = Q.Budget(cfg("query_time_budget_s"))
    left = limit_of(p)
    out = {"cols": Q.FACE_COLS, "parts": []}
    for part in parts_from(p.get("paths")):
        rows, scanned, matched = Q.face_rows(
            part, kind=kind, near=near, radius=p.get("radius"), normal=p.get("normal"),
            angle_tol=float(p.get("angle_tol", 5.0)), limit=left, budget=b)
        left -= len(rows)
        rec = {"path": Q.safe(lambda: part.pathName), "scanned": scanned, "matched": matched, "rows": rows}
        out["parts"].append(rec)
        if b.hit:
            break
    if b.hit:
        out["budget_exceeded"] = "narrow with paths, near/radius or kind"
    return out


_CONN_GETTERS = ["getFasteners", "getBolt3Ds", "getRigidLinks", "getBushings", "getConnectors",
                 "getConnectorDiscretes", "getNodeTies", "getDiscreteTies",
                 "getMeshDependentTies", "getInteractions", "getPointMasses"]
_CONN_PROPS = ["connectorType", "diameter", "stiffness", "preload", "dependentDof",
               "distributionType", "end1AttachmentRegion", "end2AttachmentRegion",
               "attachmentRegions", "contactBody1", "contactBody2", "bodies"]


def _call_getter(fn, model):
    for target in ([{"path": model.pathName}], [{"path": model.name}], []):
        try:
            return fn(target=target)
        except Exception:
            pass
    return None


def _iter_connections(model, budget):
    apex = ensure_apex()
    for name in _CONN_GETTERS:
        fn = getattr(apex.attribute, name, None)
        if fn is None:
            continue
        for e in _call_getter(fn, model) or []:
            if budget.over():
                return
            yield name[3:], e


def m_find_entities(p):
    apex = ensure_apex()
    needle = (p.get("name_contains") or "").lower()
    cap = limit_of(p)
    b = Q.Budget(cfg("query_time_budget_s"))
    out = []
    model = apex.currentModel()
    for kind, getter in (("assembly", lambda: model.getAssemblies(recursive=True)),
                         ("part", lambda: model.getParts(recursive=True))):
        for e in Q.safe(getter) or []:
            if len(out) >= cap or b.over():
                break
            if needle in (Q.safe(lambda: e.name, "") or "").lower():
                rec = S.ref(e)
                rec["kind"] = kind
                out.append(rec)
    if p.get("include_connections", True) and len(out) < cap and not b.hit:
        for kind, c in _iter_connections(model, b):
            if len(out) >= cap:
                break
            if needle in (Q.safe(lambda: c.name, "") or "").lower():
                rec = S.ref(c)
                rec["kind"] = kind
                out.append(rec)
    return {"matches": out, "truncated": len(out) >= cap or b.hit}


def m_list_connections(p):
    """Counts per connection type, then names (details=True adds a few key properties;
    collections inside them are reported as counts only)."""
    apex = ensure_apex()
    model = apex.currentModel()
    cap = limit_of(p)
    details = bool(p.get("details", False))
    props = p.get("properties") or _CONN_PROPS
    per_type = max(1, int(p.get("per_type") or 10))
    b = Q.Budget(cfg("query_time_budget_s"))
    counts, items = {}, []
    for kind, e in _iter_connections(model, b):
        counts[kind] = counts.get(kind, 0) + 1
        if len(items) < cap and counts[kind] <= per_type:
            rec = S.describe(e, [x for x in props if hasattr(e, x)], depth=0) if details else S.ref(e)
            rec["kind"] = kind
            items.append(rec)
    total = sum(counts.values())
    return {"counts_by_type": counts, "total": total, "items": items,
            "truncated": total > len(items), "budget_exceeded": b.hit}


def m_list_loads_constraints(p):
    apex = ensure_apex()
    cap = limit_of(p)
    res = {}
    for key, fn in (("loads", apex.environment.getLoads), ("constraints", apex.environment.getConstraints)):
        try:
            coll = fn() or []
            res[key + "_count"] = Q.clen(coll)
            res[key] = [S.ref(x) for i, x in enumerate(coll) if i < cap]
        except Exception as exc:
            res[key + "_error"] = str(exc)[:300]
    return res


def m_list_catalog(p):
    """Names of materials and element properties in the catalog (what can be assigned)."""
    apex = ensure_apex()
    cap = limit_of(p)
    out = {}
    for key, fn in (("materials", lambda: apex.catalog.getMaterials(target=[])),
                    ("assigned_materials", lambda: apex.catalog.getAssignedMaterials()),
                    ("properties_2d", lambda: apex.catalog.getPropertiesElement2Ds(target=[])),
                    ("properties_3d", lambda: apex.catalog.getPropertiesElement3Ds(target=[])),
                    ("layered_panels", lambda: apex.attribute.getLayeredPanels(
                        target=[{"path": apex.currentModel().pathName}]))):
        try:
            coll = fn() or []
            out[key] = {"count": Q.clen(coll),
                        "names": [Q.name_of(x) for i, x in enumerate(coll) if i < cap]}
        except Exception as exc:
            out[key] = {"error": str(exc)[:200]}
    return out


def m_list_scenarios(p):
    apex = ensure_apex()
    study = Q.safe(apex.getPrimaryStudy)
    if study is None:
        return {"study": None, "scenarios": []}
    out = {"study": S.ref(study), "scenarios": []}
    for kind, getter in (("Scenario", lambda: study.getScenarios(recursive=True)),
                         ("ScenarioNastran", lambda: study.getScenariosNastran())):
        try:
            for scn in getter() or []:  # an empty study returns None, not an empty collection
                rec = S.ref(scn)
                rec["kind"] = kind
                for attr in ("executionStatus", "solutionType"):
                    v = Q.safe(lambda a=attr: getattr(scn, a))
                    if v is not None:
                        rec[attr] = Q.enum_name(v)
                subs = Q.safe(lambda: [{"name": sc.name, "subcaseID": sc.subcaseID} for sc in scn.subcases])
                if subs:
                    rec["subcases"] = subs[:50]
                out["scenarios"].append(rec)
        except Exception as exc:
            out[kind + "_error"] = str(exc)[:300]
    return out


def m_import_fe_model(p):
    apex = ensure_apex()
    model = apex.currentModel()
    kw = dict(feModelFileName=p["model_file"], modelType=apex.mesh.FEModelFormat.NastranBulk,
              unitSystemName=p["unit_system"])
    if p.get("organize_by"):
        kw["organizeBy"] = enum(apex.mesh.FEModelImportOrganizationOptions, p["organize_by"], "organize_by")
    ent = model.importFEModel(**kw)
    return {"created": S.ref(ent)}


def nastran_scenarios(study):
    return list(Q.safe(lambda: study.getScenariosNastran()) or [])


def m_export_bdf(p):
    """Write the Nastran deck of a Nastran scenario (the only one, or p['scenario']); with
    no scenario in the study, write the model's bulk data. The server summarises the file."""
    apex = ensure_apex()
    study = Q.safe(apex.getPrimaryStudy)
    scns = nastran_scenarios(study) if study is not None else []
    name = p.get("scenario")
    if name:
        scn = next((s for s in scns if Q.name_of(s) == name), None)
        if scn is None:
            raise KeyError("no Nastran scenario '%s'. Have: %s" % (name, [Q.name_of(s) for s in scns]))
    elif len(scns) > 1:
        raise ValueError("several Nastran scenarios, pass scenario=: %s" % [Q.name_of(s) for s in scns])
    else:
        scn = scns[0] if scns else None
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in (Q.name_of(scn) if scn else "model"))
    path = p.get("path") or os.path.join(out_dir("bdf"), stem + ".bdf")
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    chosen = p.get("unit_system") or CFG.get("script_unit_system")
    units = chosen or apex.getScriptUnitSystemLabel()
    kw = {"filename": path, "unitSystem": units}
    if scn is not None and p.get("hdf5", True):
        kw["resultOutputType"] = apex.attribute.NastranResultOutputType.Hdf5
    t0 = time.time()
    try:
        (scn if scn is not None else apex.currentModel()).exportFEModel(**kw)
    except RuntimeError as exc:
        if "units system" not in str(exc):
            raise
        raise ValueError("Apex cannot export a deck in '%s'%s. Pass unit_system with a consistent system, "
                         "e.g. mm-t-s-N-K or m-kg-s-N-K, in the units the model was built for."
                         % (units, "" if chosen else ", its current script units")) from exc
    out = {"path": path, "scenario": Q.name_of(scn) if scn else None, "unit_system": units,
           "bytes": os.path.getsize(path), "seconds": round(time.time() - t0, 2)}
    if scn is None:
        out["note"] = "no Nastran scenario in the study: bulk data only, no case control"
    return out


# ----------------------------------------------------------------- raw
SCRIPT = "<script>"  # filename scripts are compiled under; their traceback frames show this


def _inject(ns, name, mod):
    """Put a helper module in the script namespace unless the script bound that name itself
    (a reloaded copy of the same module still counts as ours)."""
    cur = ns.get(name)
    if cur is None or (isinstance(cur, types.ModuleType) and cur.__name__ == mod.__name__):
        ns[name] = mod


def script_traceback(exc):
    """Traceback limited to the user's script frames plus the exception line: the worker's
    exec frame and Apex's internal (smodules) frames say nothing about the script."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == SCRIPT]
    lines = (["Traceback (script frames):\n"] + traceback.format_list(frames)) if frames else []
    return "".join(lines + traceback.format_exception_only(type(exc), exc))


def script_error(exc, code):
    """{type, message, line, source, calls}: where the script failed and which apex calls are on
    that line, resolved in the failing frame (so the server can attach their signatures)."""
    info = {"type": type(exc).__name__, "message": str(exc)[:600]}
    last, tb = None, exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == SCRIPT:
            last = tb
        tb = tb.tb_next
    if last is None:
        line = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
        if line:
            info["line"] = line
        return info
    info["line"] = last.tb_lineno
    lines = code.splitlines()
    if 0 < last.tb_lineno <= len(lines):
        info["source"] = lines[last.tb_lineno - 1].strip()[:300]
    try:
        info["calls"] = IN.failing_calls(code, last.tb_lineno, last.tb_frame, ensure_apex(),
                                         position=IN.tb_position(last))
    except Exception as e:  # never let the diagnosis hide the error itself
        info["calls_error"] = str(e)[:200]
    return info


def _snapshot():
    try:
        return FX.snapshot(sys.modules[__name__], float(cfg("effects_budget_s")), bool(cfg("effects_mesh_counts")))
    except Exception as exc:
        log("effects snapshot failed", exc)
        return {"failed": str(exc)[:200]}


def _effects(before):
    if "failed" in before:
        return {"error": "snapshot before the script failed: " + before["failed"]}
    after = _snapshot()
    if "failed" in after:
        return {"error": "snapshot after the script failed: " + after["failed"]}
    return FX.receipt(before, after)


def m_run_apex_python(p):
    if STATE["ns"] is None:
        STATE["ns"] = {"__name__": "__mcp__"}
    ns = STATE["ns"]
    _inject(ns, "Q", Q)
    _inject(ns, "S", S)
    if p.get("attach", True):
        ns["apex"] = ensure_apex()
    code = p["code"]
    query = p.get("mode") == "query"  # query_apex_python: the server checked it reads only
    log_path = os.path.join(out_dir("scripts"), "%s_%s.py" % ("query" if query else "script", stamp()))
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("# %s %s\n%s\n" % ("query_apex_python" if query else "run_apex_python",
                                    datetime.datetime.now().isoformat(), code))
    # source for the traceback lines (mtime None: linecache.checkcache leaves it alone)
    linecache.cache[SCRIPT] = (len(code), None, code.splitlines(True), SCRIPT)
    ns.pop("result", None)
    # a query is always snapshotted: the receipt is what shows it really changed nothing
    track = (query or bool(p.get("effects", True))) and p.get("attach", True)
    before = _snapshot() if track else None
    buf = io.StringIO()
    err = info = None
    t0 = time.time()
    with contextlib.redirect_stdout(buf):
        try:
            exec(compile(code, SCRIPT, "exec"), ns)  # noqa: S102 - the user asked for a raw tool
        except BaseException as exc:  # sys.exit() too: its output and error must still come back
            err = script_traceback(exc)
            info = script_error(exc, code)
    out = {"stdout": buf.getvalue()[-20000:], "seconds": round(time.time() - t0, 3), "logged_to": log_path}
    if before is not None:  # also after a failure: a script that raised halfway may have changed things
        out["effects"] = _effects(before)
    if "result" in ns:
        res = S.to_json(ns["result"], depth=4)
        size_kb = len(json.dumps(res, default=str)) / 1024.0
        room_kb = float(cfg("max_response_kb")) - len(out["stdout"]) / 1024.0 - 4  # stdout still fits
        if size_kb > room_kb:
            out["result_error"] = ("result too large (%.0f kB, max_response_kb %.0f): aggregate in the "
                                   "script and return counts / min / max / top-N"
                                   % (size_kb, float(cfg("max_response_kb"))))
            out["result_preview"] = json.dumps(res, default=str)[:2000]
        else:
            out["result"] = res
    if err:
        out["traceback"] = err[-8000:]
        out["error"] = info
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n# --- outcome: %s, %.3fs\n" % ("error" if err else "ok", out["seconds"]))
    return out


METHODS = {name[2:]: fn for name, fn in globals().items() if name.startswith("m_") and callable(fn)}
METHODS.update({name: (lambda fn: lambda p: fn(sys.modules[__name__], p))(fn) for name, fn in B.TOOLS.items()})
METHODS.update({name: (lambda fn: lambda p: fn(sys.modules[__name__], p))(fn) for name, fn in SC.TOOLS.items()})
METHODS.update({name: (lambda fn: lambda p: fn(sys.modules[__name__], p))(fn) for name, fn in IN.TOOLS.items()})


def handle_request(req):
    """One request dict -> one response dict. Never raises; caps the response size."""
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    t0 = time.time()
    try:
        if method not in METHODS:
            raise KeyError("unknown method '%s'. Known: %s" % (method, sorted(METHODS)))
        res = S.to_json(METHODS[method](params), depth=50)
        size_kb = len(json.dumps(res, default=str)) / 1024.0
        cap_kb = float(cfg("max_response_kb"))
        if size_kb > cap_kb:
            log("trimmed", method, "%.0f kB" % size_kb)
            res = {"error": "response too large (%.0f kB > max_response_kb %.0f). Narrow the query "
                            "(paths, limit, details=False) or aggregate inside run_apex_python."
                            % (size_kb, cap_kb),
                   "preview": json.dumps(res, default=str)[:4000]}
        log("ok", method, "%.2fs" % (time.time() - t0))
        return {"id": rid, "ok": True, "result": res}
    except BaseException as exc:
        log("error", method, exc)
        if getattr(exc, "quiet", False):  # an expected refusal or lookup miss: the message says it all
            return {"id": rid, "ok": False, "error": str(exc)}
        return {"id": rid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc()[-6000:]}
