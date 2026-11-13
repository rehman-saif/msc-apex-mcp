"""Effects receipt for run_apex_python: what a script changed in the model (Apex script thread).

A snapshot before and after the script records what can be read cheaply:
  * assemblies and parts by pathName, with per-part solid / surface / curve / mesh counts and
    element / node totals (from the bulk ID arrays, never element objects);
  * loads, constraints, connections, materials, element properties and scenarios by name.
The diff names what was created, deleted or changed. A script that "ran fine" but did nothing,
or failed halfway after changing things, shows up here instead of going unnoticed.

Not tracked: property values, assignments, coordinates, visibility and colours (a rename shows
as one deletion plus one creation). Every snapshot runs under a time budget; a section that did
not finish is reported as untracked instead of producing a wrong diff.
"""
import time
from collections import Counter

import queries as Q

MAX_NAMES = 20  # per list in the receipt

PART_COUNTS = (("solids", "getSolids"), ("surfaces", "getSurfaces"), ("curves", "getCurves"),
               ("meshes", "getMeshes"))


def _path(e):
    return Q.safe(lambda: e.pathName) or Q.name_of(e)


def _names(coll):
    return [str(_path(e)) for e in (coll or [])]


def _part_counts(part, mesh_counts):
    rec = {}
    for key, getter in PART_COUNTS:
        fn = getattr(part, getter, None)
        if fn is not None:
            rec[key] = Q.clen(Q.safe(fn))
    if mesh_counts:
        ne = nn = 0
        for m in Q.safe(lambda: part.getMeshes()) or []:
            by_topology, _ = Q.topology_counts(m)
            ne += sum(by_topology.values())
            ids = Q.safe(lambda: m.getNodeIDs())
            nn += len(ids) if ids is not None else 0
        rec["elements"], rec["nodes"] = ne, nn
    return rec


def _sections(W, apex, model, budget):
    """(name, getter) for the named sections; each getter returns a list of names."""
    study = Q.safe(apex.getPrimaryStudy)

    def scenarios():
        out = []
        if study is not None:
            for getter in (lambda: study.getScenarios(recursive=True), lambda: study.getScenariosNastran()):
                out += _names(Q.safe(getter))
        return out

    def connections():
        return ["%s:%s" % (kind, _path(e)) for kind, e in W._iter_connections(model, budget)]

    def properties():
        out = []
        for name in ("getPropertiesElement2Ds", "getPropertiesElement3Ds"):
            fn = getattr(apex.catalog, name, None)
            if fn is not None:
                out += _names(Q.safe(lambda: fn(target=[])))
        return out

    return (("loads", lambda: _names(apex.environment.getLoads())),
            ("constraints", lambda: _names(apex.environment.getConstraints())),
            ("materials", lambda: _names(apex.catalog.getMaterials(target=[]))),
            ("properties", properties),
            ("connections", connections),
            ("scenarios", scenarios))


def snapshot(W, budget_s=5.0, mesh_counts=True):
    apex = W.ensure_apex()
    b = Q.Budget(budget_s)
    t0 = time.time()
    snap = {"partial": [], "errors": {}}
    model = Q.safe(apex.currentModel)
    snap["model"] = _path(model) if model is not None else None
    if model is None:
        return snap
    snap["assemblies"] = _names(Q.safe(lambda: model.getAssemblies(recursive=True)))
    parts = {}
    for p in Q.safe(lambda: model.getParts(recursive=True)) or []:
        if b.over():
            snap["partial"].append("parts")
            break
        try:
            parts[str(_path(p))] = _part_counts(p, mesh_counts)
        except Exception as exc:  # one odd part must not cost the whole receipt
            parts[str(_path(p))] = {"error": type(exc).__name__}
    snap["parts"] = parts
    for name, getter in _sections(W, apex, model, b):
        if b.over():
            snap["partial"].append(name)
            continue
        try:
            snap[name] = getter()
        except Exception as exc:  # a getter Apex does not have, or one that raises on this model
            snap["errors"][name] = str(exc)[:200]
        if name == "connections" and b.hit:
            snap["partial"].append(name)  # _iter_connections stops early when the budget runs out
    snap["seconds"] = round(time.time() - t0, 3)
    return snap


def _capped(names):
    names = sorted(names)
    return names if len(names) <= MAX_NAMES else names[:MAX_NAMES] + ["... +%d more" % (len(names) - MAX_NAMES)]


def _multiset_diff(a, b):
    ca, cb = Counter(a or []), Counter(b or [])
    return list((cb - ca).elements()), list((ca - cb).elements())


def diff(before, after):
    """{created: {section: [names]}, deleted: {...}, changed: {part: {count: [before, after]}},
    untracked: [...]}; empty sections are left out."""
    if before.get("model") != after.get("model"):
        return {"model_switched": [before.get("model"), after.get("model")]}
    untracked = sorted(set(before.get("partial", [])) | set(after.get("partial", []))
                       | set(before.get("errors", {})) | set(after.get("errors", {})))
    created, deleted, changed = {}, {}, {}
    for name in ("assemblies", "loads", "constraints", "materials", "properties", "connections", "scenarios"):
        if name in untracked or name not in before or name not in after:
            continue
        new, gone = _multiset_diff(before[name], after[name])
        if new:
            created[name] = _capped(new)
        if gone:
            deleted[name] = _capped(gone)
    if "parts" not in untracked and "parts" in before and "parts" in after:
        bp, ap = before["parts"], after["parts"]
        new = [p for p in ap if p not in bp]
        gone = [p for p in bp if p not in ap]
        if new:
            created["parts"] = _capped(new)
        if gone:
            deleted["parts"] = _capped(gone)
        for p in sorted(set(bp) & set(ap)):
            d = {k: [bp[p].get(k), v] for k, v in ap[p].items() if bp[p].get(k) != v}
            if d:
                changed[p] = d
        if len(changed) > MAX_NAMES:
            keep = sorted(changed)[:MAX_NAMES]
            changed = dict({k: changed[k] for k in keep}, **{"...": "+%d more parts" % (len(changed) - MAX_NAMES)})
    out = {}
    for key, val in (("created", created), ("deleted", deleted), ("changed", changed)):
        if val:
            out[key] = val
    if untracked:
        out["untracked"] = untracked
    return out


def receipt(before, after):
    """The diff plus the time both snapshots took."""
    out = diff(before, after)
    out["seconds"] = round(before.get("seconds", 0) + after.get("seconds", 0), 3)
    return out
