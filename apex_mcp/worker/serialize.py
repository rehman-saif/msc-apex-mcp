"""Turn Apex API objects into small JSON-safe structures.

Nothing here imports apex, so it can be unit-tested without Apex.
Every attribute read is guarded: an Apex property that raises is reported
as an error string instead of breaking the whole response.
"""
import math

MAX_ITEMS = 25


def _is_entity(v):
    return hasattr(v, "entityType") or hasattr(v, "getEntityType")


def _is_collection(v):
    return hasattr(v, "len") and hasattr(v, "__iter__") and not isinstance(v, (str, bytes, dict))


def _xyz(v):
    try:
        return [float(v.x), float(v.y), float(v.z)]
    except Exception:
        return None


def entity_type(e):
    for attr in ("entityType",):
        try:
            t = getattr(e, attr)
            s = str(t)
            return s.split(".")[-1]
        except Exception:
            pass
    return type(e).__name__


def ref(e):
    """Short reference to an entity: name, path, type, id."""
    out = {"type": entity_type(e), "class": type(e).__name__}
    for key, attr in (("name", "name"), ("path", "pathName"), ("id", "id")):
        try:
            val = getattr(e, attr)
            if val is not None and val != "":
                out[key] = val if isinstance(val, (int, float, str)) else str(val)
        except Exception:
            pass
    return out


def to_json(v, depth=1):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    try:
        import numpy as np  # noqa: WPS433
        if isinstance(v, np.generic):
            return to_json(v.item(), depth)
        if isinstance(v, np.ndarray):
            return to_json(v.tolist(), depth)
    except Exception:
        pass
    if isinstance(v, dict):
        return {str(k): to_json(x, depth - 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        items = list(v)
        out = [to_json(x, depth - 1) for x in items[:200]]
        if len(items) > 200:
            out.append("... %d more" % (len(items) - 200))
        return out
    # Bounding box
    if hasattr(v, "point1") and hasattr(v, "point2"):
        return {"min": _xyz(v.point1), "max": _xyz(v.point2)}
    if _is_entity(v):
        return ref(v)
    if _is_collection(v):
        try:
            n = v.len()
        except Exception:
            n = None
        if depth <= 0:
            return {"count": n}
        items = []
        try:
            for i, x in enumerate(v):
                if i >= MAX_ITEMS:
                    break
                items.append(ref(x) if _is_entity(x) else to_json(x, 0))
        except Exception as exc:  # pragma: no cover
            items.append("iteration failed: %s" % exc)
        return {"count": n, "items": items, "truncated": bool(n and n > MAX_ITEMS)}
    xyz = _xyz(v)
    if xyz is not None:
        return xyz
    # SWIG maps / vectors
    try:
        if hasattr(v, "keys"):
            return {str(k): to_json(v[k], depth - 1) for k in list(v.keys())}
    except Exception:
        pass
    try:
        if hasattr(v, "__len__") and hasattr(v, "__getitem__"):
            return [to_json(v[i], depth - 1) for i in range(min(len(v), 200))]
    except Exception:
        pass
    return str(v).split(".")[-1] if "." in str(v) and " " not in str(v) else str(v)


def describe(e, props, depth=1):
    """ref(e) plus the listed properties, each read defensively."""
    out = ref(e)
    for p in props:
        try:
            out[p] = to_json(getattr(e, p), depth)
        except Exception as exc:
            out[p] = "<error: %s>" % str(exc)[:120]
    return out
