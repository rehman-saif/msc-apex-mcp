"""Model-building helpers for scripts: `B` in the run_apex_python namespace.

    B.create_material(name="Ti64", E=113800.0, nu=0.342, rho=4.43e-9)
    B.assign_solid_property(paths=["base/Asm"], material="Ti64")
    B.mesh_solid(paths=["base/Asm"], size=3.0)

Thin wrappers over calls proven in real Apex 2024.1 sessions:
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

Every helper CHANGES THE MODEL and is idempotent: calling it again with the same name (for rbe2,
the same reference node) replaces what the earlier call made instead of adding a duplicate. They
return ids and counts only.

Targets: a pathName (the whole part/solid/surface), {"path": part, "faces": [face ids]} (ids from
the list_faces tool), {"path": part, "nodes": [node ids]}, or a list of these.
"""
import queries as Q

DOF_KEYS = ("translationX", "translationY", "translationZ", "rotationX", "rotationY", "rotationZ")


def _W():
    """The worker module (it imports this one, so it is looked up when a helper runs)."""
    import apex_worker
    return apex_worker


# ------------------------------------------------------------------ internals
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
    """Three floats, checked before a helper deletes what it is about to replace."""
    try:
        vec = [float(c) for c in values]
    except (TypeError, ValueError):
        vec = []
    if len(vec) != 3:
        raise ValueError("%s needs three numbers: got %r" % (what, values))
    return vec


def _model_getter(fn):
    W = _W()
    return list(W._call_getter(fn, W.ensure_apex().currentModel()) or [])


def _ensure_part(path):
    """(part, created): the part at path, created under its parent assembly if missing."""
    apex = _W().ensure_apex()
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
        raise KeyError("no material '%s' (B.create_material first)" % name)
    return m


def _solids(paths):
    out = []
    for e in _W().parts_from(paths):
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


# ------------------------------------------------------------------ helpers
def targets(spec):
    """apex.EntityCollection from a target spec: a pathName (the whole part/solid/surface),
    {"path": part, "faces": [ids]}, {"path": part, "nodes": [ids]}, or a list of these. Reads only."""
    W = _W()
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


def create_material(name, E, nu, rho=None, extra=None):
    """Create or update an isotropic MAT1 material in script units (e.g. mm-t-s-N: E in MPa, rho in
    t/mm^3). extra: further MAT1 fields by Apex name."""
    apex = _W().ensure_apex()
    props = {"E": float(E), "NU": float(nu)}
    if rho is not None:
        props["RHO"] = float(rho)
    props.update(extra or {})
    mat = _material(apex, name)
    created = mat is None
    if created:
        mat = apex.catalog.createMaterial(name=name, primaryMaterialModel="MAT1")
    mat.getMaterialModel(materialModel="MAT1").update(materialProperties=props)
    return {"material": name, "id": mat.id, "created": created}


def assign_solid_property(paths, material, name=None):
    """Create or update a PSOLID (default name PSOLID_<material>) pointing at `material` and assign
    it to every solid of the given parts/assemblies (or the given solids)."""
    apex = _W().ensure_apex()
    mat = _need_material(apex, material)
    name = name or "PSOLID_" + material
    prop = Q.safe(lambda: apex.catalog.getPropertiesElement3DDictionary()[name])
    if prop is None:
        prop = _named(Q.safe(lambda: Q.element_properties(apex, "3D")), name)
    created = prop is None
    if created:
        prop = apex.catalog.createPropertiesElement3D(name=name, primaryProperties3D="PSOLID")
    prop.getProperties3DModel(properties3DModel="PSOLID").update(elementProperties3D={"MID": mat.id})
    solids = _solids(paths)
    apex.attribute.assignPropertiesElement3D(target=Q.collection(solids), property=prop)
    return {"property": name, "id": prop.id, "material_id": mat.id, "solids": len(solids), "created": created}


def mesh_solid(paths, size, order="Quadratic", name=None):
    """Tet-mesh the solids of each part (createSolidMesh, order Quadratic | Linear), first deleting
    the meshes already on those solids, so a re-mesh replaces rather than duplicates. Ties to mesh
    nodes of a replaced mesh are lost; ties to faces survive. Returns element/node counts per part."""
    W = _W()
    apex = W.ensure_apex()
    size = float(size)
    order = W.enum(apex.mesh.ElementOrder, order or "Quadratic", "order")
    out = []
    for part in W.parts_from(paths):
        rec = {"path": Q.safe(lambda: part.pathName)}
        solids = list(Q.safe(lambda: part.getSolids()) or [])
        if not solids:
            rec["skipped"] = "no solids"
            out.append(rec)
            continue
        keys = {Q.safe(lambda s=s: s.pathName) for s in solids}
        rec["replaced"] = _delete(apex, [m for m in Q.meshes_of(part) if _geometry_path(m) in keys])
        apex.mesh.createSolidMesh(name=name or "Mesh_" + Q.name_of(part),
                                  target=Q.collection(solids), meshSize=size, elementOrder=order)
        mine = [m for m in Q.meshes_of(part) if _geometry_path(m) in keys]
        rec["meshes"] = len(mine)
        rec["elements"], rec["nodes"] = _mesh_counts(mine)
        others = len(Q.meshes_of(part)) - len(mine)
        if others:
            rec["other_meshes"] = others  # not tied to these solids, left alone
        out.append(rec)
    return {"parts": out}


def glue(name, side1, side2, tolerance=0.5, type="Glue"):
    """Manual interaction pair between two targets (typically faces: {"path": part, "faces": [ids]}),
    with a manual contact tolerance (script length units). type: apex.attribute.InteractionType
    member. Nastran ignores it without BCONTACT in the subcases (static_scenario sets it)."""
    W = _W()
    apex = W.ensure_apex()
    itype = W.enum(apex.attribute.InteractionType, type or "Glue", "type")
    s1, s2 = targets(side1), targets(side2)
    old = _named(_model_getter(apex.attribute.getInteractions), name)
    bodies = []  # Apex leaves the old pair's contact bodies behind, and they export as unused BCSURFs
    if old is not None:
        for getter in ("getContactBodies1", "getContactBodies2"):
            bodies += list(Q.safe(lambda g=getter: getattr(old, g)()) or [])
    replaced = _delete(apex, [old] + bodies)
    kw = {"contactToleranceCalculationMethod": apex.AutoManual.Manual, "contactTolerance": float(tolerance)}
    try:
        geo = apex.attribute.createInteractionPropertyGeometric(name=name + "_geom", **kw)
    except Exception:  # name left over from an earlier call: let Apex name it
        geo = apex.attribute.createInteractionPropertyGeometric(**kw)
    ia = apex.attribute.createInteractionManualPair(name=name, targetSide1=s1, targetSide2=s2)
    ia.createInteractionRep(name=name + "_rep", interactionType=itype, interactionPropertyGeometric=geo)
    return {"interaction": name, "id": Q.safe(lambda: ia.id), "side1": s1.len(), "side2": s2.len(),
            "replaced": bool(replaced)}


def _ref_node(ref, node_part, tol):
    """(node, created) for rbe2's ref: {"path": part, "node": id} or [x, y, z] in node_part."""
    W = _W()
    apex = W.ensure_apex()
    if isinstance(ref, dict):
        return list(Q.nodes_by_id(W.one_entity(ref["path"]), [ref["node"]]))[0], False
    xyz = [float(c) for c in ref]
    if not node_part:
        raise ValueError("ref=[x,y,z] needs node_part: the part to find (within tol) or create the node in")
    part, _ = _ensure_part(node_part)
    node = Q.node_near(part, xyz, tol)
    if node is not None:
        return node, False
    return apex.mesh.createNodeByLocation(coordinates=apex.Coordinate(*xyz), pathName=part.pathName), True


def rbe2(ref, attach, dof="123456", node_part=None, tol=0.01):
    """Rigid node tie (RBE2) from a reference node to `attach` (a target: faces or nodes). ref is
    {"path": part, "node": id} (e.g. a beam end node from beam) or [x, y, z]: an existing node of
    node_part within tol, else a new node there (node_part is created if missing). A tie already
    on that reference node is replaced."""
    apex = _W().ensure_apex()
    tol = float(tol)
    region = targets(attach)
    node, created = _ref_node(ref, node_part, tol)
    nid = int(node.id)
    old = [t for t in _model_getter(apex.attribute.getNodeTies)
           if Q.safe(lambda t=t: int(t.getReferencePoint().id)) == nid]
    if old:
        _delete(apex, old)
        node, created = _ref_node(ref, node_part, tol)  # deleting a tie can delete its free reference node
    tie = apex.attribute.createNodeTie(referencePoint=node, dependentDoF=str(dof or "123456"),
                                       attachmentRegions=region,
                                       distributionType=apex.attribute.DistributionType.Rigid)
    return {"tie_id": Q.safe(lambda: tie.id), "ref_node": int(node.id), "ref_node_created": created,
            "dependent_nodes": len(Q.safe(tie.getAttachmentDOFAndWeights) or {}),
            "regions": region.len(), "replaced": len(old)}


def beam(name, points, radius, material, mesh_size, part, order="Linear"):
    """Solid-round beam (e.g. bolt shank or pin) along a polyline through `points`, in `part`
    (created if missing): curve, round BeamShape, free BeamSpan with `material`, curve mesh named
    BeamMesh_<name>. Returns nodes_at_points: the mesh node id at each point, ready for rbe2's ref."""
    W = _W()
    apex = W.ensure_apex()
    pts = [[float(c) for c in pt] for pt in points]
    if len(pts) < 2:
        raise ValueError("points needs at least two [x, y, z]")
    mat = _need_material(apex, material)
    part, _ = _ensure_part(part)
    mesh_name = "BeamMesh_" + name

    def ours(m):
        n = Q.name_of(m) or ""
        return n == mesh_name or n.startswith(mesh_name + " ")
    old = [m for m in Q.meshes_of(part) if ours(m)]
    replaced = _delete(apex, old + [_geometry_body(m) for m in old])  # the meshes and their curves
    model = apex.currentModel()
    # createCurve builds in the current part; setCurrentPart wants the path below the model:
    # 'Asm/Part', not 'base/Asm/Part' ("Specified part is invalid in this Model", Apex 2024.1)
    model.setCurrentPart(pathName=part.pathName[len(model.pathName) + 1:])
    locs = apex.ILocationCollection()
    locs.appendList([apex.Coordinate(*x) for x in pts])
    curve = apex.geometry.createCurve(target=locs, behavior=apex.geometry.CurveBehavior.Polyline)
    Q.safe(lambda: curve.update(name=name))
    radius = float(radius)
    shape_name = "Round_R%g" % radius
    shape = Q.safe(lambda: apex.catalog.getBeamShapeDictionary()[shape_name])
    if shape is None:
        shape = apex.attribute.createBeamShapeSolidRound(name=shape_name, radius=radius)
    edges = apex.geometry.EdgeCollection()
    edges.appendList(list(curve.getEdges()))
    spans = apex.attribute.createBeamSpanFree(name="Span_" + name, beamTarget=edges, shapeEndA=shape, material=mat)
    order = W.enum(apex.mesh.ElementOrder, order or "Linear", "order")
    apex.mesh.createCurveMesh(name=mesh_name, target=Q.collection([curve]), meshSize=float(mesh_size),
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


def constrain(name, target, type="Clamped", dof=None, id=None):
    """Displacement constraint (SPC) on a target. type: Clamped | Symmetry | Spherical | Axial |
    General. dof (e.g. "123") fixes only those components through type General; that path is not
    yet proven in Apex, so check the SPC cards of the exported deck. Re-creating keeps the old SPC
    set id unless id is given."""
    W = _W()
    apex = W.ensure_apex()
    if dof and not set(str(dof)) <= set("123456"):
        raise ValueError("dof must use the digits 1-6, e.g. \"123\": got %r" % (dof,))
    tgt = targets(target)
    kw = {"name": name, "target": tgt, "applicationMethod": apex.attribute.ApplicationMethod.Direct}
    if dof:
        kw["constraintType"] = apex.attribute.ConstraintType.General
        kw.update({key: 0.0 for d, key in zip("123456", DOF_KEYS) if d in str(dof)})
    else:
        kw["constraintType"] = W.enum(apex.attribute.ConstraintType, type or "Clamped", "type")
    old = _named(apex.environment.getConstraints() or [], name)
    if id is not None:
        kw["id"] = int(id)
    elif old is not None:
        kw["id"] = int(old.id)  # keep the SPC set id that scenarios already point at
    replaced = _delete(apex, [old])
    c = apex.environment.createConstraintDisplacement(**kw)
    return {"constraint": name, "id": Q.safe(lambda: c.id), "targets": tgt.len(), "replaced": bool(replaced)}


def nodal_load(name, target, force=None, moment=None, id=None):
    """Force [fx, fy, fz] or moment [mx, my, mz] (one per call; each is one Nastran LOAD set) applied
    directly to a target, usually {"path": part, "nodes": [id]}. Re-creating keeps the old LOAD set
    id unless id is given."""
    apex = _W().ensure_apex()
    if (force is None) == (moment is None):
        raise ValueError("give force=[fx,fy,fz] or moment=[mx,my,mz]: one per load, since each load "
                         "is one Nastran LOAD set id")
    x, y, z = _vec3("force" if force is not None else "moment", force if force is not None else moment)
    tgt = targets(target)
    kw = {"name": name, "target": tgt, "applicationMethod": apex.attribute.ApplicationMethod.Direct}
    old = _named(apex.environment.getLoads() or [], name)
    if id is not None:
        kw["id"] = int(id)
    elif old is not None:
        kw["id"] = int(old.id)  # keep the LOAD set id that scenarios already point at
    replaced = _delete(apex, [old])
    if force is not None:
        load = apex.environment.createLoadForceComponent(forceX=x, forceY=y, forceZ=z, **kw)
    else:
        load = apex.environment.createLoadMomentComponent(momentX=x, momentY=y, momentZ=z, **kw)
    return {"load": name, "id": Q.safe(lambda: load.id), "targets": tgt.len(), "replaced": bool(replaced)}


def _set_command(sub, cmd, value):
    # value first, then activate: the order that persisted in Apex 2024.1
    c = sub.getCommand(commandName=cmd)
    if cmd == "BCONTACT":  # setValue(value: str): an int is stored as nothing and exports as BCONTACT = USER
        c.setValue(str(value))
    else:
        try:
            c.setValue(value)
        except Exception:
            c.setValue(str(value) if not isinstance(value, str) else int(value))
    sub.setCommand(commandName=cmd, activate=True)


def _contact_id():
    """BCONTACT for the model's interactions: the id of their InteractionRep, which Apex 2024.1
    writes as the BCONECT id. None without interactions. One BCONTACT selects one id, so
    interactions on several ids need an explicit choice."""
    ids = sorted({Q.safe(lambda ia=ia: int(ia.getActiveRep().id))
                  for ia in _model_getter(_W().ensure_apex().attribute.getInteractions)} - {None})
    if len(ids) > 1:
        raise ValueError("the model has interactions on several contact ids %s, and one BCONTACT selects "
                         "one id: pass bcontact= (per subcase or for all) and check the exported deck" % ids)
    return ids[0] if ids else None


def static_scenario(name, subcases, model_rep=None, bcontact="auto"):
    """SOL 101 Nastran scenario with one subcase per entry of subcases=[{"name", "load",
    "constraint", "bcontact"?}] (load / constraint by name). Sets LOAD, SPC and BCONTACT per
    subcase and reads them back. bcontact "auto" sets BCONTACT to the contact id of the model's
    interactions (the id of their InteractionRep, which Apex writes as the BCONECT id), and leaves it
    off without interactions; an int or a keyword ("ALLBODY", "NONE") sets that; None leaves it off. model_rep: assembly path (default: the only top-level
    assembly). Replaces a scenario of the same name."""
    W = _W()
    apex = W.ensure_apex()
    subs = subcases or []
    if not subs:
        raise ValueError("subcases: [{name, load, constraint}, ...] needs at least one entry")
    model = apex.currentModel()
    tables = {"load": {Q.name_of(x): x for x in apex.environment.getLoads() or []},
              "constraint": {Q.name_of(x): x for x in apex.environment.getConstraints() or []}}
    bc = bcontact
    if bc == "auto":
        bc = _contact_id()
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
    if model_rep:
        rep = W.one_entity(model_rep)
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
