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

CFG = {}
MODE = {"inprocess": False, "log_file": None}
STATE = {"ns": None, "apex": None}

DEFAULTS = {"query_time_budget_s": 20.0, "max_items": 200, "max_response_kb": 256,
            "effects_budget_s": 5.0, "effects_mesh_counts": True}


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
        STATE["apex"] = None  # ensure_apex applies script_unit_system again
    MODE["inprocess"] = True
    MODE["log_file"] = os.path.join(out_dir("bridge"), "bridge_worker.log")


def out_dir(kind):
    d = os.path.join(CFG.get("output_dir") or os.path.join(HERE, "..", "..", "output"), kind)
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


class Refusal(ValueError):
    """An expected refusal: the message says it all, so no traceback goes back with it."""
    quiet = True


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
    if STATE["apex"] is not None:
        return STATE["apex"]
    import apex
    STATE["apex"] = apex
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
        raise KeyError("not found: %s (model_overview gives the exact pathNames)" % missing)
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
    """What the server needs for apex_status. `root` lets it see that Apex runs this copy."""
    apex = ensure_apex()
    model = apex.currentModel()
    out = {"root": ROOT, "application": S.to_json(apex.getApplicationInfo()),
           "model": Q.safe(lambda: model.pathName) if model is not None else None}
    for k, fn in (("script_units", "getScriptUnitSystemLabel"),
                  ("application_units", "getApplicationUnitSystemLabel"),
                  ("project_folder", "getCurrentProjectFolderPath")):
        try:
            out[k] = getattr(apex, fn)()
        except Exception as exc:
            out[k] = "<error: %s>" % exc
    return out


TREE_COLS = ["path", "solids", "surfaces", "meshes", "elements", "nodes", "visible"]


def _part_row(part, apex):
    """One row of the overview's parts, in the order of TREE_COLS."""
    row = [Q.safe(lambda: part.pathName)]
    for getter in ("getSolids", "getSurfaces", "getMeshes"):
        row.append(Q.clen(Q.safe(lambda g=getter: getattr(part, g)())))
    # element/node totals from the bulk ID arrays of each mesh (no element objects)
    ne = nn = 0
    for m in Q.safe(lambda: part.getMeshes()) or []:
        c, _ = Q.topology_counts(m)
        ne += sum(c.values())
        ids = Q.safe(lambda: m.getNodeIDs())
        nn += len(ids) if ids is not None else 0
    vis = Q.enum_name(Q.safe(lambda: part.getVisibility()), Q.safe(lambda: apex.Visibility))
    return row + [ne, nn, vis != "Hidden"]


def _tree(apex, model, counts, cap, b):
    """One row per part. The assembly tree is in the paths; assemblies that hold no part anywhere
    below them are listed separately, since no part path shows them."""
    all_parts = list(Q.safe(lambda: model.getParts(recursive=True)) or [])
    rows = []
    for pt in all_parts:
        if len(rows) >= cap or b.over():
            break
        rows.append(_part_row(pt, apex) if counts else Q.safe(lambda: pt.pathName))
    out = {"cols": TREE_COLS} if counts else {}
    out["parts"] = rows
    if len(rows) < len(all_parts):
        out["parts_truncated"] = "%d of %d parts listed%s" % (len(rows), len(all_parts),
                                                               "; time budget reached" if b.hit else "; raise limit")
    part_paths = [Q.safe(lambda: pt.pathName) or "" for pt in all_parts]
    empty = []
    for a in Q.safe(lambda: model.getAssemblies(recursive=True)) or []:
        path = Q.safe(lambda: a.pathName)
        if path and not any(pp.startswith(path + "/") for pp in part_paths):
            empty.append(path)
    if empty:
        out["empty_assemblies"] = empty[:cap]
    return out


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


PER_TYPE = 10  # connections listed per type; the counts are always complete


def _connections(apex, model, cap, b):
    counts, items = {}, []
    for kind, e in _iter_connections(model, b):
        counts[kind] = counts.get(kind, 0) + 1
        if len(items) < cap and counts[kind] <= PER_TYPE:
            rec = S.ref(e)
            rec["type"] = kind
            items.append(rec)
    out = {"count": sum(counts.values()), "by_type": counts, "items": items}
    if out["count"] > len(items):
        out["note"] = "first %d of each type listed" % PER_TYPE
    return out


def _listed(coll, cap):
    coll = coll or []
    return {"count": Q.clen(coll), "items": [S.ref(x) for i, x in enumerate(coll) if i < cap]}


def _catalog(apex, model, cap, b):
    """Names of materials and element properties in the catalog (what can be assigned)."""
    out = {}
    for key, fn in (("materials", lambda: apex.catalog.getMaterials(target=[])),
                    ("assigned_materials", lambda: apex.catalog.getAssignedMaterials()),
                    ("properties_2d", lambda: Q.element_properties(apex, "2D")),
                    ("properties_3d", lambda: Q.element_properties(apex, "3D")),
                    ("layered_panels", lambda: apex.attribute.getLayeredPanels(target=[{"path": model.pathName}]))):
        try:
            coll = fn() or []
            out[key] = {"count": Q.clen(coll), "names": [Q.name_of(x) for i, x in enumerate(coll) if i < cap]}
        except Exception as exc:
            out[key] = {"error": str(exc)[:200]}
    return out


def _scenarios(apex, model, cap, b):
    study = Q.safe(apex.getPrimaryStudy)
    out = []
    if study is None:
        return out
    studies = Q.safe(lambda: apex.studies)
    enums = {"executionStatus": Q.safe(lambda: studies.ExecutionStatus),
             "solutionType": Q.safe(lambda: studies.NastranSolutionType)}
    # an empty study returns None, not an empty collection
    for kind, getter in (("Scenario", lambda: study.getScenarios(recursive=True)),
                         ("ScenarioNastran", lambda: study.getScenariosNastran())):
        for scn in getter() or []:
            rec = {"type": kind, "name": Q.name_of(scn), "path": Q.safe(lambda: scn.pathName)}
            for attr in ("executionStatus", "solutionType"):
                v = Q.safe(lambda a=attr: getattr(scn, a))
                if v is not None:  # ints in Apex: named through the enum class (Nastran scenarios)
                    rec[attr] = Q.enum_name(v, enums[attr] if kind == "ScenarioNastran" or attr == "executionStatus"
                                            else None)
            subs = Q.safe(lambda: [{"name": sc.name, "id": sc.subcaseID} for sc in scn.subcases])
            if subs:
                rec["subcases"] = subs[:50]
            out.append(rec)
    return out


OVERVIEW = {  # section -> (apex, model, cap, budget) -> content; "parts" is handled apart (it has `counts`)
    "connections": _connections,
    "loads": lambda apex, model, cap, b: _listed(apex.environment.getLoads(), cap),
    "constraints": lambda apex, model, cap, b: _listed(apex.environment.getConstraints(), cap),
    "catalog": _catalog,
    "scenarios": _scenarios,
}
SECTIONS = ("parts",) + tuple(OVERVIEW)


def m_model_overview(p):
    """What the model holds, by section. A section that fails is reported, the others still come back."""
    apex = ensure_apex()
    sections = list(p.get("sections") or SECTIONS)
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        raise Refusal("unknown section(s) %s. Sections: %s" % (unknown, ", ".join(SECTIONS)))
    b = Q.Budget(cfg("query_time_budget_s"))
    cap = limit_of(p)
    model = apex.currentModel()
    out = {"model": Q.safe(lambda: model.pathName)}
    for name in sections:
        try:
            if name == "parts":
                out.update(_tree(apex, model, bool(p.get("counts", True)), cap, b))
            else:
                out[name] = OVERVIEW[name](apex, model, cap, b)
        except Exception as exc:
            out[name + "_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    if b.hit:
        out["budget_exceeded"] = "query_time_budget_s reached: ask for fewer sections"
    return out


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
        raise Refusal("Apex cannot export a deck in '%s'%s. Pass unit_system with a consistent system, "
                         "e.g. mm-t-s-N-K or m-kg-s-N-K, in the units the model was built for."
                         % (units, "" if chosen else ", its current script units")) from exc
    if not os.path.isfile(path):  # Apex 2024.1 returned without error and wrote nothing (unmeshed model)
        elements = sum(sum(Q.topology_counts(m)[0].values())
                       for p in Q.safe(lambda: apex.currentModel().getParts(recursive=True)) or []
                       for m in Q.safe(lambda: p.getMeshes()) or [])
        raise Refusal("Apex reported no error but wrote no deck to %s.%s" % (
            path, " The model has no mesh (0 elements), so there may be nothing to export." if elements == 0
            else " Check the scenario %s in Apex." % (repr(Q.name_of(scn)) if scn is not None else "")))
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


HELPER_FILES = ("builders.py", "queries.py")  # B and Q: their frames say which apex call failed inside a helper


def script_traceback(exc):
    """Traceback limited to the script's frames and the B / Q helpers' plus the exception line: the
    worker's exec frame and Apex's internal (smodules) frames say nothing about the script."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if f.filename == SCRIPT or os.path.basename(f.filename) in HELPER_FILES]
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


def script_namespace():
    """The namespace scripts run in (kept between calls), with apex and the helpers in it."""
    if STATE["ns"] is None:
        STATE["ns"] = {"__name__": "__mcp__"}
    ns = STATE["ns"]
    _inject(ns, "Q", Q)  # read helpers
    _inject(ns, "B", B)  # model-building helpers
    _inject(ns, "S", S)
    ns["apex"] = ensure_apex()
    return ns


def m_run_apex_python(p):
    ns = script_namespace()
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
    track = query or bool(p.get("effects", True))
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
