"""Memory-safe model queries for Apex (imported in Apex's CPython as `Q`).

Why this exists
---------------
Walking `part.elements` / `mesh.elements` and reading per-element properties
(material, elementProperty2D, ...) creates one Python wrapper per element and an
attribute-resolution chain per read. On a 90k-element mesh that exhausted Apex's
memory and crashed it. Everything here uses the *bulk* MeshBody arrays instead:

    mesh.getElementIDs()            {ElementTopology: [ids]}      (counts by topology)
    mesh.getElementConnectivities() {ElementTopology: [node idx]} (flat, by index)
    mesh.getNodeIDs()               [ids]
    mesh.getNodeCoordinates()       [x1,y1,z1,x2,...]

and reads element attributes only for a handful of *sampled* elements.

Rules for any custom query script (run_apex_python)
---------------------------------------------------
1. Never iterate part.elements, part.nodes, mesh.elements, mesh.nodes or
   solid.faces of a large model in Python. Use the bulk arrays above.
2. Get materials/properties per MeshBody / GeometryBody, or sample a few
   elements with Q.sample_elements(mesh, n).
3. Aggregate inside the script (counts, min/max, top-N) and return small
   results. Never return raw ID or coordinate lists longer than a few hundred.
4. Put any unavoidable loop under a Budget:  b = Q.Budget(15); ... if b.over(): break

Nothing here changes the model.
"""
import math
import time

MAX_ITEMS = 200


class Budget:
    """Wall-clock guard for loops on Apex's script thread."""

    def __init__(self, seconds=20.0):
        self.t0 = time.time()
        self.limit = float(seconds)
        self.hit = False

    def over(self):
        if time.time() - self.t0 > self.limit:
            self.hit = True
        return self.hit

    def elapsed(self):
        return round(time.time() - self.t0, 3)


# ------------------------------------------------------------------ small utils
def name_of(v):
    if v is None:
        return None
    for attr in ("name", "pathName"):
        try:
            s = getattr(v, attr)
            if s:
                return str(s)
        except Exception:
            pass
    s = str(v)
    return s.split(".")[-1] if "." in s and " " not in s else s


def enum_name(v):
    return str(v).split(".")[-1]


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def clen(coll):
    """Length of an Apex collection or Python sequence, None if unknown."""
    if coll is None:
        return 0
    for fn in (lambda: coll.len(), lambda: len(coll)):
        try:
            return int(fn())
        except Exception:
            pass
    return None


def bbox(entity):
    b = safe(lambda: entity.getAxisAlignedBoundingBox())
    if b is None:
        return None
    try:
        lo = [float(b.point1.x), float(b.point1.y), float(b.point1.z)]
        hi = [float(b.point2.x), float(b.point2.y), float(b.point2.z)]
        return {"min": [round(x, 4) for x in lo], "max": [round(x, 4) for x in hi],
                "size": [round(h - l, 4) for l, h in zip(lo, hi)]}
    except Exception:
        return None


def rle(ids):
    """[3,4,5,9] -> '3-5,9-9' (Apex range string for getElements/getNodes(ids=...))."""
    ids = sorted(set(int(i) for i in ids))
    out, start, prev = [], None, None
    for i in ids:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append("%d-%d" % (start, prev))
            start = prev = i
    if start is not None:
        out.append("%d-%d" % (start, prev))
    return ",".join(out)


_rle = rle  # old private name, kept for scripts that used it


def collection(items):
    """apex.EntityCollection holding items (a plain list outside Apex)."""
    try:
        import apex
        coll = apex.EntityCollection()
        coll.appendList(list(items))
        return coll
    except ImportError:
        return list(items)


# Apex 2024.1 does not expose apex.geometry.GeometryFaultType; order as documented.
_FAULT_TYPES = ["Sliver", "SmallEdge", "SmallFace", "Overhang", "Gap", "Crack", "Spike"]
_TOPO = {}


def fault_type_name(v):
    try:
        i = int(v)
        return _FAULT_TYPES[i] if 0 <= i < len(_FAULT_TYPES) else str(i)
    except Exception:
        return enum_name(v)


def topology_name(v):
    """ElementTopology int or enum -> 'Quad4', 'Tria3', ..."""
    if not _TOPO:
        try:
            import apex
            cls = apex.mesh.ElementTopology
            for n in dir(cls):
                if not n.startswith("_"):
                    try:
                        _TOPO[int(getattr(cls, n))] = n
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        return _TOPO.get(int(v), str(v))
    except Exception:
        return enum_name(v)


# ------------------------------------------------------------------ meshes
def topology_counts(mesh):
    """{topology_name: count} from the bulk ID map - no element objects."""
    out = {}
    ids = safe(lambda: mesh.getElementIDs(), None)
    if ids is None:
        ids = safe(lambda: mesh.elementIDs, {}) or {}
    try:
        keys = list(ids.keys())
    except Exception:
        keys = []
    for k in keys:
        try:
            out[topology_name(k)] = len(ids[k])
        except Exception:
            pass
    return out, ids


def element_ids_flat(mesh):
    _, ids = topology_counts(mesh)
    flat = []
    try:
        for k in list(ids.keys()):
            flat.extend(int(x) for x in ids[k])
    except Exception:
        pass
    return flat


def get_elements_by_ids(mesh, id_list):
    """ElementCollection for a short list of element IDs of one mesh."""
    s = rle(id_list)
    for call in (lambda: mesh.getElements(ids=s),
                 lambda: mesh.getElements(ids=s, indices=""),
                 lambda: mesh.getElements(ids=s, indices=None)):
        try:
            return call()
        except Exception:
            pass
    return None


def element_record(el):
    rec = {"id": safe(lambda: int(el.id))}
    rec["topology"] = topology_name(safe(lambda: el.topology, "?"))
    rec["property_2d"] = name_of(safe(lambda: el.elementProperty2D))
    rec["property_3d"] = name_of(safe(lambda: el.elementProperty3D))
    rec["material"] = name_of(safe(lambda: el.material))
    t = safe(lambda: float(el.thickness))
    rec["thickness"] = None if (t is None or math.isnan(t)) else round(t, 5)
    return {k: v for k, v in rec.items() if v is not None}


def sample_elements(mesh, n=3):
    """Attributes of n evenly spaced elements of a mesh (n capped at 50)."""
    n = max(0, min(int(n), 50))
    if n == 0:
        return []
    flat = element_ids_flat(mesh)
    if not flat:
        return []
    step = max(1, len(flat) // n)
    pick = flat[::step][:n]
    coll = get_elements_by_ids(mesh, pick)
    if coll is None:
        return [{"error": "could not fetch sample elements by id"}]
    out = []
    for i, el in enumerate(coll):
        if i >= n:
            break
        out.append(element_record(el))
    return out


def mesh_summary(mesh, sample=3):
    counts, _ = topology_counts(mesh)
    rec = {"name": name_of(mesh), "class": type(mesh).__name__,
           "elements": sum(counts.values()), "by_topology": counts}
    nids = safe(lambda: mesh.getNodeIDs())
    rec["nodes"] = len(nids) if nids is not None else None
    geo = safe(lambda: mesh.getAssociatedGeometry())
    rec["geometry"] = name_of(geo) if geo is not None else "(orphan mesh)"
    for key, attr in (("mesh_size", "meshSize"), ("element_order", "elementOrder"),
                      ("mesh_type", "meshType")):
        v = safe(lambda a=attr: getattr(mesh, a))
        if v is not None:
            rec[key] = v if isinstance(v, (int, float)) else enum_name(v)
    if sample:
        rec["sample_elements"] = sample_elements(mesh, sample)
    return rec


def node_bbox_numpy(mesh):
    """Bounding box from the bulk coordinate array (numpy if present)."""
    xyz = safe(lambda: mesh.getNodeCoordinates())
    if not xyz:
        return None
    try:
        import numpy as np
        a = np.asarray(xyz, dtype=float).reshape(-1, 3)
        return {"min": a.min(0).round(4).tolist(), "max": a.max(0).round(4).tolist()}
    except Exception:
        xs, ys, zs = xyz[0::3], xyz[1::3], xyz[2::3]
        return {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]}


# ------------------------------------------------------------------ nodes
def meshes_of(entity):
    """The MeshBodies of a part (or [entity] if it already is a mesh)."""
    if hasattr(entity, "getMeshes"):
        return list(safe(lambda: entity.getMeshes()) or [])
    return [entity]


def node_arrays(mesh):
    """(ids, xyz) numpy arrays of one mesh, from the bulk arrays."""
    import numpy as np
    ids = np.asarray(list(mesh.getNodeIDs() or []), dtype=np.int64)
    xyz = np.asarray(list(mesh.getNodeCoordinates() or []), dtype=float).reshape(-1, 3)
    return ids, xyz


def nodes_where(entity, fn, ids_only=False):
    """Nodes of a mesh (or of every mesh of a part) where fn(xyz) is True. fn gets an (N,3)
    numpy array and returns a bool mask, e.g. a bolt-hole wall:
        Q.nodes_where(mesh, lambda p: abs(np.hypot(p[:,0]-x, p[:,1]-y) - r) < 0.05)
    Returns an EntityCollection of Nodes, or sorted node IDs with ids_only=True."""
    import numpy as np
    ids_out, nodes = [], []
    for m in meshes_of(entity):
        ids, xyz = node_arrays(m)
        if not len(ids):
            continue
        sel = ids[np.asarray(fn(xyz), dtype=bool)]
        if not len(sel):
            continue
        ids_out.extend(int(i) for i in sel)
        if not ids_only:
            nodes.extend(list(m.getNodes(ids=rle(sel))))
    return sorted(ids_out) if ids_only else collection(nodes)


def nodes_by_id(entity, ids):
    """EntityCollection of the Nodes with these IDs in a part's meshes. KeyError if any is missing."""
    want = set(int(i) for i in ids)
    found, nodes = set(), []
    for m in meshes_of(entity):
        have = want.intersection(int(i) for i in (m.getNodeIDs() or []))
        if have:
            found |= have
            nodes.extend(list(m.getNodes(ids=rle(have))))
    if found != want:
        raise KeyError("node ids not in %s: %s" % (name_of(entity), sorted(want - found)[:20]))
    return collection(nodes)


def node_near(entity, xyz, tol=0.01):
    """The Node of a part's meshes nearest to xyz if it lies within tol, else None."""
    import numpy as np
    best = None
    for m in meshes_of(entity):
        ids, pts = node_arrays(m)
        if not len(ids):
            continue
        d = np.linalg.norm(pts - np.asarray(xyz, dtype=float), axis=1)
        i = int(np.argmin(d))
        if d[i] <= tol and (best is None or d[i] < best[0]):
            best = (float(d[i]), m, int(ids[i]))
    return best[1].getNode(id=best[2]) if best else None


def nodes_on_face(faces, ids_only=False):
    """Mesh nodes associated with one Face or several (Face.getNodes, de-duplicated)."""
    if hasattr(faces, "getNodes"):
        faces = [faces]
    seen, nodes = set(), []
    for f in faces:
        for n in f.getNodes():
            i = int(n.id)
            if i not in seen:
                seen.add(i)
                nodes.append(n)
    return sorted(seen) if ids_only else collection(nodes)


# ------------------------------------------------------------------ faces
def bodies_of(entity):
    """Solids and surfaces of a part, or [entity] for a single body."""
    if hasattr(entity, "getSolids"):
        return list(safe(lambda: entity.getSolids()) or []) + list(safe(lambda: entity.getSurfaces()) or [])
    return [entity]


def faces_by_id(entity, ids):
    """EntityCollection of the Faces with these IDs on a part's (or one body's) solids and
    surfaces, in the order given. KeyError if any is missing."""
    want = [int(i) for i in ids]
    found = {}
    for body in bodies_of(entity):
        for f in safe(lambda: body.getFaces()) or []:
            i = safe(lambda: int(f.id))
            if i in want:
                found[i] = f
    missing = [i for i in want if i not in found]
    if missing:
        raise KeyError("face ids not in %s: %s (use list_faces)" % (name_of(entity), missing[:20]))
    return collection([found[i] for i in want])


def _vec(v):
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except Exception:
        return None


def _unit(v):
    n = math.sqrt(sum(c * c for c in v)) if v else 0.0
    return [c / n for c in v] if n > 0 else None


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _uv_range(face):
    pr = safe(lambda: face.getParametricRange()) or safe(lambda: face.parametricRange) or {}
    r = {}
    for k, v in (safe(lambda: dict(pr).items()) or []):
        k = str(k).lower()
        side = "lo" if ("start" in k or "min" in k) else "hi" if ("end" in k or "max" in k) else None
        if side and k[:1] in "uv":
            r[k[0] + side] = float(v)
    return r if len(r) == 4 else None


def face_shape(face):
    """('plane', normal, None) | ('cyl', axis, radius) | ('curved', None, None) | ('?', None, None).
    Classified from normals sampled over the face's parametric range: Apex reports a
    cylindricalAxis for every face, so that alone does not say the face is a cylinder."""
    r = _uv_range(face)
    if r is None:
        return "?", None, None
    uv = [(r["ulo"] + a * (r["uhi"] - r["ulo"]), r["vlo"] + b * (r["vhi"] - r["vlo"]))
          for a, b in ((0.5, 0.5), (0.2, 0.2), (0.8, 0.2), (0.2, 0.8), (0.8, 0.8))]
    normals = [_unit(_vec(safe(lambda u=u, v=v: face.evaluateNormal(u=u, v=v)))) for u, v in uv]
    if any(n is None for n in normals):
        return "?", None, None
    if all(abs(_dot(normals[0], n)) > 0.9999 for n in normals):
        return "plane", normals[0], None
    axis = _unit(_vec(safe(lambda: face.getCylindricalAxis())))
    mid = _vec(safe(lambda: face.getCylindricalAxisMidPoint()))
    if axis and mid and all(abs(_dot(axis, n)) < 1e-3 for n in normals):
        radii = []
        for u, v in uv:
            p = _vec(safe(lambda u=u, v=v: face.evaluateFaceParametricCoordinate(u=u, v=v)))
            if p is None:
                break
            d = [pc - mc for pc, mc in zip(p, mid)]
            t = _dot(d, axis)
            radii.append(math.sqrt(max(0.0, _dot(d, d) - t * t)))
        if len(radii) == len(uv) and max(radii) - min(radii) <= 1e-3 * max(1.0, max(radii)):
            return "cyl", axis, sum(radii) / len(radii)
    return "curved", None, None


FACE_COLS = ["id", "kind", "area", "centroid", "dir", "r"]


def face_rows(entity, kind=None, near=None, radius=None, normal=None, angle_tol=5.0,
              limit=MAX_ITEMS, budget=None):
    """Compact rows (FACE_COLS) for the faces of a part's bodies that pass the filters.
    dir is the plane normal or cylinder axis; r the cylinder radius. near+radius keeps faces
    whose centroid lies within radius of near; normal keeps faces whose dir is within
    angle_tol degrees of it (either sense). Returns (rows, faces_scanned, faces_matched)."""
    want_n = _unit([float(c) for c in normal]) if normal else None
    cos_tol = math.cos(math.radians(float(angle_tol)))
    rows, scanned, matched = [], 0, 0
    for body in bodies_of(entity):
        for f in safe(lambda: body.getFaces()) or []:
            if budget is not None and budget.over():
                return rows, scanned, matched
            scanned += 1
            c = _vec(safe(lambda: f.centroid)) or _vec(safe(lambda: f.getCentroid()))
            if near is not None and (c is None or math.dist(c, near) > float(radius)):
                continue
            shape, d, r = face_shape(f) if (kind or want_n or len(rows) < limit) else ("?", None, None)
            if kind and shape != kind:
                continue
            if want_n and (d is None or abs(_dot(d, want_n)) < cos_tol):
                continue
            matched += 1
            if len(rows) < limit:
                a = safe(lambda: float(f.area))
                rows.append([safe(lambda: int(f.id)), shape, round(a, 4) if a is not None else None,
                             [round(x, 3) for x in c] if c else None,
                             [round(x, 4) for x in d] if d else None,
                             round(r, 4) if r is not None else None])
    return rows, scanned, matched


# ------------------------------------------------------------------ geometry
def solid_summary(solid, budget=None, edge_stats=True, with_bbox=True):
    rec = {"name": name_of(solid), "id": safe(lambda: int(solid.id))}
    v = safe(lambda: float(solid.getVolume()))
    if v is None:
        v = safe(lambda: float(solid.volume))
    rec["volume"] = round(v, 4) if v is not None else None
    faces = safe(lambda: solid.getFaces())
    edges = safe(lambda: solid.getEdges())
    rec["faces"] = clen(faces) if faces is not None else None
    rec["edges"] = clen(edges) if edges is not None else None
    rec["cells"] = clen(safe(lambda: solid.getCells()))
    if edge_stats and edges is not None:
        rec.update(edge_length_stats(edges, budget))
    if faces is not None:
        rec.update(face_area_stats(faces, budget))
    if with_bbox:
        rec["bbox"] = bbox(solid)
    return rec


def surface_summary(surf, budget=None, edge_stats=True):
    rec = {"name": name_of(surf), "id": safe(lambda: int(surf.id))}
    a = safe(lambda: float(surf.getArea()))
    rec["area"] = round(a, 4) if a is not None else None
    rec["faces"] = clen(safe(lambda: surf.getFaces()))
    ext = safe(lambda: surf.getExteriorEdges())
    rec["free_edges"] = clen(ext) if ext is not None else None
    rec["manifold"] = safe(lambda: bool(surf.isManifold))
    edges = safe(lambda: surf.getEdges())
    rec["edges"] = clen(edges) if edges is not None else None
    if edge_stats and edges is not None:
        rec.update(edge_length_stats(edges, budget))
    rec["bbox"] = bbox(surf)
    return rec


def edge_length_stats(edges, budget=None, limit=20000):
    lengths, n = [], 0
    for e in edges:
        n += 1
        if n > limit or (budget is not None and budget.over()):
            break
        L = safe(lambda: float(e.length))
        if L is not None:
            lengths.append(L)
    if not lengths:
        return {}
    lengths.sort()
    return {"edge_len_min": round(lengths[0], 5), "edge_len_max": round(lengths[-1], 4),
            "edge_len_median": round(lengths[len(lengths) // 2], 4),
            "edges_scanned": len(lengths)}


def face_area_stats(faces, budget=None, limit=20000):
    areas, n = [], 0
    for f in faces:
        n += 1
        if n > limit or (budget is not None and budget.over()):
            break
        A = safe(lambda: float(f.area))
        if A is not None:
            areas.append(A)
    if not areas:
        return {}
    areas.sort()
    return {"face_area_min": round(areas[0], 6), "face_area_max": round(areas[-1], 4)}


def part_summary(part, sample=3, geometry=True, budget_s=20.0, max_items=MAX_ITEMS):
    """Everything needed to judge a part, without touching element objects in bulk."""
    b = Budget(budget_s)
    rec = {"name": name_of(part), "path": safe(lambda: part.pathName), "bbox": bbox(part)}
    meshes = safe(lambda: part.getMeshes()) or []
    rec["mesh_count"] = clen(meshes)
    mlist = []
    for i, m in enumerate(meshes):
        if i >= max_items or b.over():
            rec["meshes_truncated"] = True
            break
        mlist.append(mesh_summary(m, sample))
    rec["meshes"] = mlist
    rec["elements_total"] = sum(m.get("elements", 0) for m in mlist)
    rec["nodes_total"] = sum(m.get("nodes") or 0 for m in mlist)
    if geometry:
        sol = safe(lambda: part.getSolids()) or []
        srf = safe(lambda: part.getSurfaces()) or []
        rec["solid_count"], rec["surface_count"] = clen(sol), clen(srf)
        rec["solids"], rec["surfaces"] = [], []
        for i, s in enumerate(sol):
            if i >= max_items or b.over():
                rec["solids_truncated"] = True
                break
            # per-solid bbox left out: it nearly always repeats the part bbox
            rec["solids"].append(solid_summary(s, b, edge_stats=False, with_bbox=False))
        for i, s in enumerate(srf):
            if i >= max_items or b.over():
                rec["surfaces_truncated"] = True
                break
            rec["surfaces"].append(surface_summary(s, b, edge_stats=False))
    rec["seconds"] = b.elapsed()
    if b.hit:
        rec["budget_exceeded"] = True
    return rec


def geometry_bodies(part):
    out = []
    for getter in ("getSolids", "getSurfaces"):
        coll = safe(lambda g=getter: getattr(part, g)()) or []
        for x in coll:
            out.append(x)
    return out
