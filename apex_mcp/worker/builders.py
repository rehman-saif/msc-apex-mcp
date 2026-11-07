"""Idempotent model builders, one MCP tool each (run in-process on Apex's script thread).

Thin wrappers over calls proven in real Apex 2024.1 sessions (output/scripts):
  MAT1          catalog.createMaterial + getMaterialModel("MAT1").update(materialProperties=)
  PSOLID        catalog.createPropertiesElement3D + getProperties3DModel("PSOLID").update(MID=)
                + attribute.assignPropertiesElement3D
  solid mesh    mesh.createSolidMesh
  glue          attribute.createInteractionManualPair + createInteractionRep(Glue)
  RBE2          mesh.createNodeByLocation + attribute.createNodeTie(Rigid)
  beam          geometry.createCurve(Polyline) + attribute.createBeamSpanFree + mesh.createCurveMesh
  SPC           environment.createConstraintDisplacement
  FORCE/MOMENT  environment.createLoadForceComponent / createLoadMomentComponent
  SOL 101       Study.createScenarioNastran + per-subcase LOAD / SPC / BCONTACT commands

Calling a builder again with the same name (or, for rbe2, the same reference node)
replaces what the earlier call made instead of adding a duplicate. Results are ids and
counts only. Every function takes (W, p); W is the apex_worker module.
"""
import queries as Q

DOF_KEYS = ("translationX", "translationY", "translationZ", "rotationX", "rotationY", "rotationZ")


# ------------------------------------------------------------------ helpers
def _named(items, name):
    for e in items or []:
        if Q.safe(lambda e=e: e.name) == name:
            return e
    return None


def _delete(apex, ents):
    seen, keep = set(), []  # one Python wrapper per lookup, so compare what Apex identifies them by
    for e in ents:
        key = e is not None and (type(e).__name__, Q.safe(lambda e=e: e.id), Q.safe(lambda e=e: e.pathName))
        if key and key not in seen:
            seen.add(key)
            keep.append(e)
    if keep:
        apex.deleteEntities(target=Q.collection(keep))
    return len(keep)


def _vec3(what, values):
    """Three floats, checked before a builder deletes what it is about to replace."""
    try:
        vec = [float(c) for c in values]
    except (TypeError, ValueError):
        vec = []
    if len(vec) != 3:
        raise ValueError("%s needs three numbers: got %r" % (what, values))
    return vec


def _model_getter(W, fn):
    return list(W._call_getter(fn, W.ensure_apex().currentModel()) or [])


def targets(W, spec):
    """EntityCollection from a target spec: a pathName (the whole part/solid/surface), or
    {"path": part, "faces": [ids]}, or {"path": part, "nodes": [ids]}, or a list of these."""
    if isinstance(spec, (str, dict)):
        spec = [spec]
    items = []
    for t in spec or []:
        if isinstance(t, str):
            items.extend(list(W.entities_by_path([t])))
            continue
        ent = W.one_entity(t["path"])
        if t.get("faces"):
            items.extend(list(Q.faces_by_id(ent, t["faces"])))
        elif t.get("nodes"):
            items.extend(list(Q.nodes_by_id(ent, t["nodes"])))
        else:
            items.append(ent)
    if not items:
        raise ValueError("empty target")
    return Q.collection(items)


def ensure_part(W, path):
    """(part, created): the part at path, created under its parent assembly if missing."""
    apex = W.ensure_apex()
    part = Q.safe(lambda: apex.getPart(pathName=path))
    if part is not None:
        return part, False
    parent, _, leaf = path.rpartition("/")
    model = apex.currentModel()
    asm = model if parent in (Q.safe(lambda: model.pathName), Q.safe(lambda: model.name)) else \
        Q.safe(lambda: apex.getAssembly(pathName=parent))
    if asm is None or not leaf:
        raise KeyError("no part '%s' and no assembly '%s' to create it in" % (path, parent))
    return asm.createPart(name=leaf), True


def _material(apex, name):
    m = Q.safe(lambda: apex.catalog.getMaterialDictionary()[name])
    return m if m is not None else _named(Q.safe(lambda: apex.catalog.getMaterials(target=[])), name)


def _need_material(apex, name):
    m = _material(apex, name)
    if m is None:
        raise KeyError("no material '%s' (create_material first)" % name)
    return m


def _solids(W, paths):
    out = []
    for e in W.parts_from(paths):
        out.extend(list(Q.safe(lambda e=e: e.getSolids()) or []) if hasattr(e, "getSolids") else [e])
    if not out:
        raise ValueError("no solids under %s" % paths)
    return out


def _geometry_body(mesh):
    geo = Q.safe(lambda: mesh.getAssociatedGeometry())
    return Q.safe(lambda: geo.getBody()) if geo is not None and hasattr(geo, "getBody") else geo


def _geometry_path(mesh):
    body = _geometry_body(mesh)
    return Q.safe(lambda: body.pathName)


def _mesh_counts(meshes):
    ne = nn = 0
    for m in meshes:
        c, _ = Q.topology_counts(m)
        ne += sum(c.values())
        nn += len(Q.safe(lambda m=m: m.getNodeIDs()) or [])
    return ne, nn


# ------------------------------------------------------------------ builders
def create_material(W, p):
    apex = W.ensure_apex()
    name = p["name"]
    props = {"E": float(p["E"]), "NU": float(p["nu"])}
    if p.get("rho") is not None:
        props["RHO"] = float(p["rho"])
    props.update(p.get("extra") or {})
    mat = _material(apex, name)
    created = mat is None
    if created:
        mat = apex.catalog.createMaterial(name=name, primaryMaterialModel="MAT1")
    mat.getMaterialModel(materialModel="MAT1").update(materialProperties=props)
    return {"material": name, "id": mat.id, "created": created}


def assign_solid_property(W, p):
    apex = W.ensure_apex()
    mat = _need_material(apex, p["material"])
    name = p.get("name") or "PSOLID_" + p["material"]
    prop = Q.safe(lambda: apex.catalog.getPropertiesElement3DDictionary()[name])
    if prop is None:
        prop = _named(Q.safe(lambda: apex.catalog.getPropertiesElement3Ds(target=[])), name)
    created = prop is None
    if created:
        prop = apex.catalog.createPropertiesElement3D(name=name, primaryProperties3D="PSOLID")
    prop.getProperties3DModel(properties3DModel="PSOLID").update(elementProperties3D={"MID": mat.id})
    solids = _solids(W, p["paths"])
    apex.attribute.assignPropertiesElement3D(target=Q.collection(solids), property=prop)
    return {"property": name, "id": prop.id, "material_id": mat.id, "solids": len(solids), "created": created}


def mesh_solid(W, p):
    apex = W.ensure_apex()
    size = float(p["size"])
    order = W.enum(apex.mesh.ElementOrder, p.get("order") or "Quadratic", "order")
    out = []
    for part in W.parts_from(p["paths"]):
        rec = {"path": Q.safe(lambda: part.pathName)}
        solids = list(Q.safe(lambda: part.getSolids()) or [])
        if not solids:
            rec["skipped"] = "no solids"
            out.append(rec)
            continue
        keys = {Q.safe(lambda s=s: s.pathName) for s in solids}
        rec["replaced"] = _delete(apex, [m for m in Q.meshes_of(part) if _geometry_path(m) in keys])
        apex.mesh.createSolidMesh(name=p.get("name") or "Mesh_" + Q.name_of(part),
                                  target=Q.collection(solids), meshSize=size, elementOrder=order)
        mine = [m for m in Q.meshes_of(part) if _geometry_path(m) in keys]
        rec["meshes"] = len(mine)
        rec["elements"], rec["nodes"] = _mesh_counts(mine)
        others = len(Q.meshes_of(part)) - len(mine)
        if others:
            rec["other_meshes"] = others  # not tied to these solids, left alone
        out.append(rec)
    return {"parts": out}


def glue(W, p):
    apex = W.ensure_apex()
    name = p["name"]
    itype = W.enum(apex.attribute.InteractionType, p.get("type") or "Glue", "type")
    s1, s2 = targets(W, p["side1"]), targets(W, p["side2"])
    replaced = _delete(apex, [_named(_model_getter(W, apex.attribute.getInteractions), name)])
    kw = {"contactToleranceCalculationMethod": apex.AutoManual.Manual,
          "contactTolerance": float(p.get("tolerance", 0.5))}
    try:
        geo = apex.attribute.createInteractionPropertyGeometric(name=name + "_geom", **kw)
    except Exception:  # name left over from an earlier call: let Apex name it
        geo = apex.attribute.createInteractionPropertyGeometric(**kw)
    ia = apex.attribute.createInteractionManualPair(name=name, targetSide1=s1, targetSide2=s2)
    ia.createInteractionRep(name=name + "_rep", interactionType=itype, interactionPropertyGeometric=geo)
    return {"interaction": name, "id": Q.safe(lambda: ia.id), "side1": s1.len(), "side2": s2.len(),
            "replaced": bool(replaced)}


def _ref_node(W, p, tol):
    """(node, created) for rbe2's ref: {"path": part, "node": id} or [x, y, z] in node_part."""
    apex = W.ensure_apex()
    ref = p["ref"]
    if isinstance(ref, dict):
        return list(Q.nodes_by_id(W.one_entity(ref["path"]), [ref["node"]]))[0], False
    xyz = [float(c) for c in ref]
    if not p.get("node_part"):
        raise ValueError("ref=[x,y,z] needs node_part: the part to find (within tol) or create the node in")
    part, _ = ensure_part(W, p["node_part"])
    node = Q.node_near(part, xyz, tol)
    if node is not None:
        return node, False
    return apex.mesh.createNodeByLocation(coordinates=apex.Coordinate(*xyz), pathName=part.pathName), True


def rbe2(W, p):
    apex = W.ensure_apex()
    tol = float(p.get("tol", 0.01))
    region = targets(W, p["attach"])
    node, created = _ref_node(W, p, tol)
    nid = int(node.id)
    old = [t for t in _model_getter(W, apex.attribute.getNodeTies)
           if Q.safe(lambda t=t: int(t.getReferencePoint().id)) == nid]
    if old:
        _delete(apex, old)
        node, created = _ref_node(W, p, tol)  # deleting a tie can delete its free reference node
    tie = apex.attribute.createNodeTie(referencePoint=node, dependentDoF=str(p.get("dof") or "123456"),
                                       attachmentRegions=region,
                                       distributionType=apex.attribute.DistributionType.Rigid)
    return {"tie_id": Q.safe(lambda: tie.id), "ref_node": int(node.id), "ref_node_created": created,
            "dependent_nodes": len(Q.safe(tie.getAttachmentDOFAndWeights) or {}),
            "regions": region.len(), "replaced": len(old)}


def beam(W, p):
    apex = W.ensure_apex()
    name = p["name"]
    pts = [[float(c) for c in pt] for pt in p["points"]]
    if len(pts) < 2:
        raise ValueError("points needs at least two [x, y, z]")
    mat = _need_material(apex, p["material"])
    part, _ = ensure_part(W, p["part"])
    mesh_name = "BeamMesh_" + name

    def ours(m):
        n = Q.name_of(m) or ""
        return n == mesh_name or n.startswith(mesh_name + " ")
    old = [m for m in Q.meshes_of(part) if ours(m)]
    replaced = _delete(apex, old + [_geometry_body(m) for m in old])  # the meshes and their curves
    apex.currentModel().setCurrentPart(pathName=part.pathName)  # createCurve builds in the current part
    locs = apex.ILocationCollection()
    locs.appendList([apex.Coordinate(*x) for x in pts])
    curve = apex.geometry.createCurve(target=locs, behavior=apex.geometry.CurveBehavior.Polyline)
    Q.safe(lambda: curve.update(name=name))
    radius = float(p["radius"])
    shape_name = "Round_R%g" % radius
    shape = Q.safe(lambda: apex.catalog.getBeamShapeDictionary()[shape_name])
    if shape is None:
        shape = apex.attribute.createBeamShapeSolidRound(name=shape_name, radius=radius)
    edges = apex.geometry.EdgeCollection()
    edges.appendList(list(curve.getEdges()))
    spans = apex.attribute.createBeamSpanFree(name="Span_" + name, beamTarget=edges, shapeEndA=shape, material=mat)
    order = W.enum(apex.mesh.ElementOrder, p.get("order") or "Linear", "order")
    apex.mesh.createCurveMesh(name=mesh_name, target=Q.collection([curve]), meshSize=float(p["mesh_size"]),
                              elementOrder=order)
    mine = [m for m in Q.meshes_of(part) if ours(m)]
    at = []
    for x in pts:
        n = next((n for n in (Q.node_near(m, x, 1e-3 * max(1.0, radius)) for m in mine) if n is not None), None)
        at.append(int(n.id) if n is not None else None)
    ne, nn = _mesh_counts(mine)
    return {"part": Q.safe(lambda: part.pathName), "curve": Q.safe(lambda: curve.pathName),
            "spans": Q.clen(spans), "elements": ne, "nodes": nn, "nodes_at_points": at,
            "replaced": bool(replaced)}


def constrain(W, p):
    apex = W.ensure_apex()
    name = p["name"]
    if p.get("dof") and not set(str(p["dof"])) <= set("123456"):
        raise ValueError("dof must use the digits 1-6, e.g. \"123\": got %r" % (p["dof"],))
    tgt = targets(W, p["target"])
    kw = {"name": name, "target": tgt, "applicationMethod": apex.attribute.ApplicationMethod.Direct}
    if p.get("dof"):
        kw["constraintType"] = apex.attribute.ConstraintType.General
        kw.update({key: 0.0 for d, key in zip("123456", DOF_KEYS) if d in str(p["dof"])})
    else:
        kw["constraintType"] = W.enum(apex.attribute.ConstraintType, p.get("type") or "Clamped", "type")
    old = _named(apex.environment.getConstraints() or [], name)
    if p.get("id") is not None:
        kw["id"] = int(p["id"])
    elif old is not None:
        kw["id"] = int(old.id)  # keep the SPC set id that scenarios already point at
    replaced = _delete(apex, [old])
    c = apex.environment.createConstraintDisplacement(**kw)
    return {"constraint": name, "id": Q.safe(lambda: c.id), "targets": tgt.len(), "replaced": bool(replaced)}


def nodal_load(W, p):
    apex = W.ensure_apex()
    name = p["name"]
    if (p.get("force") is None) == (p.get("moment") is None):
        raise ValueError("give force=[fx,fy,fz] or moment=[mx,my,mz]: one per load, since each load "
                         "is one Nastran LOAD set id")
    x, y, z = _vec3("force" if p.get("force") is not None else "moment", p.get("force") or p.get("moment"))
    tgt = targets(W, p["target"])
    kw = {"name": name, "target": tgt, "applicationMethod": apex.attribute.ApplicationMethod.Direct}
    old = _named(apex.environment.getLoads() or [], name)
    if p.get("id") is not None:
        kw["id"] = int(p["id"])
    elif old is not None:
        kw["id"] = int(old.id)  # keep the LOAD set id that scenarios already point at
    replaced = _delete(apex, [old])
    if p.get("force") is not None:
        load = apex.environment.createLoadForceComponent(forceX=x, forceY=y, forceZ=z, **kw)
    else:
        load = apex.environment.createLoadMomentComponent(momentX=x, momentY=y, momentZ=z, **kw)
    return {"load": name, "id": Q.safe(lambda: load.id), "targets": tgt.len(), "replaced": bool(replaced)}


def _set_command(sub, cmd, value):
    # value first, then activate: the order that persisted in Apex 2024.1
    c = sub.getCommand(commandName=cmd)
    try:
        c.setValue(value)
    except Exception:
        c.setValue(str(value) if not isinstance(value, str) else int(value))
    sub.setCommand(commandName=cmd, activate=True)


def static_scenario(W, p):
    apex = W.ensure_apex()
    name, subs = p["name"], p.get("subcases") or []
    if not subs:
        raise ValueError("subcases: [{name, load, constraint}, ...] needs at least one entry")
    model = apex.currentModel()
    tables = {"load": {Q.name_of(x): x for x in apex.environment.getLoads() or []},
              "constraint": {Q.name_of(x): x for x in apex.environment.getConstraints() or []}}
    bc = p.get("bcontact", "auto")
    if bc == "auto":
        bc = 1 if _model_getter(W, apex.attribute.getInteractions) else None
    elif bc in ("off", "none", None):
        bc = None
    plan = []  # resolve everything before deleting the old scenario, so a typo changes nothing
    for i, s in enumerate(subs):
        rec = {"name": s.get("name") or "Subcase %d" % (i + 1)}
        for key, cmd in (("load", "LOAD"), ("constraint", "SPC")):
            if s.get(key) is not None:
                if s[key] not in tables[key]:
                    raise KeyError("no %s named %r. Have: %s" % (key, s[key], sorted(tables[key])[:30]))
                rec[cmd] = int(tables[key][s[key]].id)
        rec["BCONTACT"] = s.get("bcontact", bc)
        plan.append(rec)
    rep_path = p.get("model_rep")
    if rep_path:
        rep = W.one_entity(rep_path)
    else:
        top = list(Q.safe(lambda: model.getAssemblies(recursive=False)) or [])
        if len(top) > 1:
            raise ValueError("several top-level assemblies, pass model_rep=: %s" % [Q.name_of(a) for a in top])
        rep = top[0] if top else model
    study = apex.getPrimaryStudy()
    replaced = _delete(apex, [s for s in W.nastran_scenarios(study) if Q.name_of(s) == name])
    sc = study.createScenarioNastran(scenarioNastranType=apex.studies.NastranSolutionType.Statics_101,
                                     numberOfSubcases=len(plan),
                                     subcaseType=apex.studies.NastranAnalysisType.Statics, name=name)
    sc.associateModelRep(modelRep=rep)
    for sub, rec in zip(sc.getSubcases(), plan):
        sub.setName(rec["name"])
        sub.setLabel(rec["name"])
        sub.setSubtitle(rec["name"])
        for cmd in ("LOAD", "SPC", "BCONTACT"):
            if rec.get(cmd) is not None:
                _set_command(sub, cmd, rec[cmd])
    # read back through a fresh handle: values set through stale command objects have been lost
    sc = next(s for s in W.nastran_scenarios(study) if Q.name_of(s) == name)
    out, bad = [], []
    for sub, rec in zip(sc.getSubcases(), plan):
        got = {"id": Q.safe(sub.getSubcaseID), "name": Q.safe(lambda: sub.name)}
        for cmd in ("LOAD", "SPC", "BCONTACT"):
            if rec.get(cmd) is not None:
                got[cmd] = Q.safe(lambda c=cmd: sub.getCommand(commandName=c).getValue())
                if str(got[cmd]) != str(rec[cmd]):
                    bad.append("%s %s=%s, wanted %s" % (got["name"], cmd, got[cmd], rec[cmd]))
        out.append(got)
    res = {"scenario": name, "model_rep": Q.safe(lambda: rep.pathName), "subcases": out,
           "replaced": bool(replaced)}
    if bad:
        res["warning"] = "read-back differs: " + "; ".join(bad)
    return res


TOOLS = {f.__name__: f for f in (create_material, assign_solid_property, mesh_solid, glue, rbe2, beam,
                                 constrain, nodal_load, static_scenario)}
