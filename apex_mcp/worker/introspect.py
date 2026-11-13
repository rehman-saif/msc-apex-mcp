"""Live view of the `apex` API as this Apex actually has it (run on Apex's script thread).

The generated API reference in the skills repo is right about most names but not all: Apex 2024.1
has documented classes and enums under other modules or not at all, enum members the reference
leaves out (`Undefined`), and 189 undocumented callables (see docs/apex-runtime-facts.md). The MCP
server's preflight checks a script's apex names against this index, so it only blocks names that
really are missing.

api_index writes one JSON file and returns its path: the index is ~100 kB, too big for a reply.
"""
import ast
import difflib
import inspect
import json
import os
import re
import time
import types

# apex.X attributes that are other Python modules imported into apex (os, sys, site, ...)
_FOREIGN = ("os", "sys", "site", "abc", "io", "enum", "builtins", "ntpath", "stat", "xml", "smerge")


def _params(fn):
    """[(name, required)] of a Python-level signature, or None when it is (*args, **kwargs)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    out = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            return None
        out.append([p.name, p.default is p.empty])
    return out


def enum_members(cls):
    """Member names if cls looks like an Apex (SWIG) enum: a class whose public attributes are ints."""
    if not isinstance(cls, type):
        return None
    names = [n for n in dir(cls) if not n.startswith("_") and n != "thisown"]
    if not names:
        return None
    for n in names:
        try:  # static: reading a property or other descriptor must not run its getter
            v = inspect.getattr_static(cls, n)
        except AttributeError:
            return None
        if not isinstance(v, int) or isinstance(v, bool):
            return None
    return names


def describe_module(mod):
    rec = {"functions": {}, "classes": [], "enums": {}, "modules": [], "other": []}
    for n in dir(mod):
        if n.startswith("_"):
            continue
        try:
            v = getattr(mod, n)
        except Exception:
            rec["other"].append(n)
            continue
        if isinstance(v, types.ModuleType):
            if v.__name__.split(".")[0] not in _FOREIGN:  # apex re-exports os, sys, site ...
                rec["modules"].append(n)
        elif isinstance(v, type):
            members = enum_members(v)
            if members is not None:
                rec["enums"][n] = members
            else:
                rec["classes"].append(n)
        elif callable(v):
            rec["functions"][n] = _params(v)
        else:
            rec["other"].append(n)
    return rec


def apex_modules(apex):
    """{'apex': apex, 'apex.mesh': <leaf.mesh>, ...}: apex and its own submodules, one level deep."""
    out = {"apex": apex}
    for n in dir(apex):
        if n.startswith("_"):
            continue
        v = getattr(apex, n, None)
        if isinstance(v, types.ModuleType) and v.__name__.split(".")[0] not in _FOREIGN:
            out["apex." + n] = v
    return out


def module_names(apex):
    """{runtime module __name__ ('leaf.mesh'): documented name ('apex.mesh')}."""
    return {m.__name__: name for name, m in apex_modules(apex).items()}


def app_info(apex):
    """getApplicationInfo() is a SWIG std::map: read it as a dict of strings."""
    try:
        app = apex.getApplicationInfo()
    except Exception:
        return {}
    for read in (lambda: dict(app), lambda: {k: app[k] for k in app.keys()}):
        try:
            return {str(k): str(v) for k, v in read().items()}
        except Exception:
            pass
    return {}


def method_names(mods):
    """Every public method name of the classes the apex modules define (Part.update, Camera.zoom,
    ...), for the read-only check of query_apex_python. Properties are left out."""
    seen, names = set(), set()
    for m in mods.values():
        for v in vars(m).values():
            if not isinstance(v, type) or id(v) in seen:
                continue
            seen.add(id(v))
            for n in dir(v):
                if n.startswith("_") or n == "thisown":
                    continue
                try:
                    raw = inspect.getattr_static(v, n)
                except AttributeError:
                    continue
                if callable(raw) or isinstance(raw, (staticmethod, classmethod)):
                    names.add(n)
    return sorted(names)


def build(apex):
    mods = apex_modules(apex)
    return {"format": 2, "application": app_info(apex),
            "modules": {name: describe_module(m) for name, m in mods.items()},
            "method_names": method_names(mods)}


# ------------------------------------------------- which apex call failed (for run_apex_python)
def _chain(node):
    """['apex', 'mesh', 'createSurfaceMesh'] for apex.mesh.createSurfaceMesh, None for anything else."""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    out.append(node.id)
    return out[::-1]


def _lookup(frame, name):
    for space in (frame.f_locals, frame.f_globals, frame.f_globals.get("__builtins__")):
        if isinstance(space, dict) and name in space:
            return True, space[name]
        if isinstance(space, types.ModuleType) and hasattr(space, name):
            return True, getattr(space, name)
    return False, None


def describe_call(func_chain, frame, modmap):
    """Resolve a called name in the failing frame: {'name': 'apex.mesh.createSurfaceMesh',
    'kind': 'function'} or {'name': 'Part.update', 'kind': 'method', 'class': 'Part',
    'method': 'update'}; None if it is not an apex call. Only names and attribute reads are
    evaluated (no calls), so resolving cannot change the model."""
    found, obj = _lookup(frame, func_chain[0])
    if not found:
        return None
    for attr in func_chain[1:-1]:
        try:
            obj = getattr(obj, attr)
        except Exception:
            return None
    last = func_chain[-1]
    if len(func_chain) == 1:  # a bare name: a function imported from apex, or one of the script's own
        mod = modmap.get(getattr(obj, "__module__", None) or "")
        return {"name": "%s.%s" % (mod, last), "kind": "function"} if mod else None
    if isinstance(obj, types.ModuleType):
        mod = modmap.get(obj.__name__)
        return {"name": "%s.%s" % (mod, last), "kind": "function"} if mod else None
    cls = obj if isinstance(obj, type) else type(obj)
    mod = modmap.get(getattr(cls, "__module__", "") or "")
    if not mod:
        return None
    return {"name": "%s.%s" % (cls.__name__, last), "kind": "method", "class": cls.__name__, "method": last,
            "module": mod, "bases": [c.__name__ for c in cls.__mro__[1:] if c is not object]}


def tb_position(tb):
    """(lineno, end_lineno, col, end_col) of the instruction that raised (Python 3.11+), else None."""
    try:
        return list(tb.tb_frame.f_code.co_positions())[tb.tb_lasti // 2]
    except Exception:
        return None


def failing_calls(code, lineno, frame, apex, position=None, limit=4):
    """The apex calls on the script line that raised: the exact call first when Python reports
    the failing instruction's position, then the other calls on that line, innermost first."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    modmap = module_names(apex)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and n.lineno <= lineno <= (getattr(n, "end_lineno", None) or n.lineno)]
    on_line = [n for n in calls if n.lineno == lineno] or calls

    def rank(n):
        exact = position is not None and tuple(position) == (n.lineno, n.end_lineno, n.col_offset, n.end_col_offset)
        return (not exact, n.end_lineno - n.lineno, -n.col_offset)
    on_line.sort(key=rank)
    out, seen = [], set()
    for n in on_line:
        chain = _chain(n.func)
        if not chain:
            continue
        rec = describe_call(chain, frame, modmap)
        if rec and rec["name"] not in seen:
            seen.add(rec["name"])
            rec["keywords"] = [k.arg for k in n.keywords if k.arg]
            out.append(rec)
        if len(out) >= limit:
            break
    return out


def api_index(W, p):
    apex = W.ensure_apex()
    idx = build(apex)
    path = os.path.join(W.out_dir("api"), "api_index.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, separators=(",", ":"))
    mods = idx["modules"]
    return {"path": path, "bytes": os.path.getsize(path), "modules": len(mods),
            "functions": sum(len(m["functions"]) for m in mods.values()),
            "enums": sum(len(m["enums"]) for m in mods.values())}


# ------------------------------------------------------------------- apex_describe
# Properties that can return every element or node of a model (or arrays as large): reading one
# builds that whole collection in Apex's memory, so describe lists them without reading them.
BIG_PROPERTY = re.compile(r"(?i:elements|nodes|entities|coordinates|connectivities)$|(IDs|Ids)$")  # not 'solids'
# Interfaces whose properties are computed on every read: IOrientation's 33 (orientation, alpha,
# euler123 ...) took 0.25 s each on a solid, 8.4 s together. Listed, read only when asked for directly.
SLOW_INTERFACES = ("IOrientation",)
_CPP = [(re.compile(r"\bconst\b|&|\*"), ""),
        (re.compile(r",\s*std::(allocator|less)<[^<>]*(<[^<>]*>)?[^<>]*>"), ""),
        (re.compile(r"ObjectSPtr<\s*([^<>]*)\s*>"), r"\1"),
        (re.compile(r"std::vector<\s*([^<>]*)\s*>"), r"[\1]"),
        (re.compile(r"std::map<\s*([^<>,]*)\s*,\s*([^<>]*)\s*>"), r"{\1:\2}"),
        (re.compile(r"std::pair<\s*([^<>,]*)\s*,\s*([^<>]*)\s*>"), r"(\1, \2)"),
        (re.compile(r"std::string"), "str"),
        (re.compile(r"\b[A-Za-z_]\w*::"), "")]


def ctype(text):
    """'std::vector< std::string,std::allocator< std::string > > const &' -> '[str]' (best effort)."""
    if not isinstance(text, str):
        text = getattr(text, "__name__", None) or str(text)
    prev = None
    while prev != text:
        prev = text
        for rx, rep in _CPP:
            text = rx.sub(rep, text)
    text = " ".join(text.split())
    for a, b in (("< ", "<"), (" >", ">"), ("[ ", "["), (" ]", "]"), ("{ ", "{"), (" }", "}"), (" :", ":")):
        text = text.replace(a, b)
    return text


def signature(fn, method=False):
    """{'params': [{'name', 'type'?, 'default'?}], 'returns'?} from the runtime signature, or
    {'star': True} when Apex exposes only (*args, **kwargs). method=True drops the self parameter."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"unknown": True}
    params = list(sig.parameters.values())
    if method and params and params[0].kind in (params[0].POSITIONAL_ONLY, params[0].POSITIONAL_OR_KEYWORD):
        params = params[1:]
    out = {"params": []}
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            return {"star": True}
        rec = {"name": p.name}
        if p.annotation is not p.empty:
            rec["type"] = ctype(p.annotation)
        if p.default is not p.empty:
            rec["default"] = repr(p.default)[:60]
        out["params"].append(rec)
    if sig.return_annotation is not sig.empty:
        out["returns"] = ctype(sig.return_annotation)
    return out


def summarize(v, ref=None, depth=1):
    """A short, JSON-able view of a property value; collections are counted, never listed in full."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v if len(v) <= 200 else v[:200] + "..."
    if ref is not None and hasattr(type(v), "pathName"):
        try:
            return ref(v)
        except Exception:
            pass
    for count in (lambda: v.len(), lambda: len(v)):
        try:
            n = int(count())
        except Exception:
            continue
        rec = {"type": type(v).__name__, "count": n}
        if isinstance(v, (list, tuple)) and v and all(isinstance(x, (bool, int, float, str)) for x in v[:12]):
            rec["values"] = list(v[:12])
        elif isinstance(v, dict):
            rec["keys"] = [str(k) for k in list(v)[:12]]
        return rec
    if hasattr(v, "point1") and hasattr(v, "point2"):  # bounding box
        try:
            return {"point1": summarize(v.point1, ref, 0), "point2": summarize(v.point2, ref, 0)}
        except Exception:
            pass
    if all(hasattr(v, a) for a in ("x", "y", "z")):
        try:
            return [getattr(v, a) for a in ("x", "y", "z")]
        except Exception:
            pass
    rec = {"type": type(v).__name__}
    if depth > 0:  # a small value object (ColorRGB, a bounding box ...): its first few properties, one level down
        props, _ = _class_members(type(v))
        for n in [n for n in props if not BIG_PROPERTY.search(n)][:8]:
            try:
                rec[n] = summarize(getattr(v, n), ref, depth - 1)
            except Exception:
                pass
    if len(rec) == 1 and "Swig Object" not in str(v):
        rec["str"] = str(v)[:120]
    return rec


def _match(names, flt):
    if not flt:
        return list(names)
    f = flt.lower()
    return [n for n in names if f in n.lower()]


def _capped(names, limit):
    names = sorted(names)
    return {"names": names[:limit], "total": len(names)} if len(names) > limit else {"names": names}


class Refused(ValueError):
    """describe will not read this (a bulk collection). quiet: the worker reports it without a traceback."""
    quiet = True


class NotFound(KeyError):
    """No such variable, name or entity. quiet as above; str() without KeyError's quotes."""
    quiet = True

    def __str__(self):
        return str(self.args[0]) if self.args else ""


def resolve(apex, target, ns=None, get_entity=None):
    """(object, owner, kind of lookup) for 'apex.mesh.X', '$var.attr' (run_apex_python namespace)
    or an entity pathName. Attribute reads only; names that could be huge collections are refused."""
    target = (target or "apex").strip()
    if target.startswith("$"):
        chain = target[1:].split(".")
        if not ns or chain[0] not in ns:
            raise NotFound("no variable %r in the run_apex_python namespace (it holds: %s)"
                           % (chain[0], ", ".join(sorted(k for k in (ns or {}) if not k.startswith("_")))[:400]))
        obj, owner = ns[chain[0]], None
        rest = chain[1:]
    elif re.match(r"^apex(\.\w+)*$", target):
        obj, owner, rest = apex, None, target.split(".")[1:]
    else:
        if get_entity is None:
            raise NotFound("not an apex path: %r" % target)
        try:
            return get_entity(target), None
        except Exception as exc:
            why = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc  # KeyError's str() adds quotes
            raise NotFound("no entity %r (%s). Entity pathNames are as list_tree returns them." % (target, why))
    done = target.split(".")[0]
    for a in rest:
        if BIG_PROPERTY.search(a) and not isinstance(obj, (type, types.ModuleType)):
            raise Refused("%r is not read: it can hold every element or node of the model. Use the bulk "
                          "getters (getElementIDs, getNodeIDs, getNodeCoordinates) or the Q helpers." % a)
        try:
            if isinstance(obj, type):
                raw = inspect.getattr_static(obj, a)
                owner, obj = obj, (raw if isinstance(raw, property) else getattr(obj, a))
            else:
                owner, obj = obj, getattr(obj, a)
        except AttributeError:
            names = [n for n in dir(obj) if not n.startswith("_") and n != "thisown"]
            low = a.lower()
            close = [n for n in names if n.lower().startswith(low)] + [n for n in names if low in n.lower()[1:]]
            close = list(dict.fromkeys(close + difflib.get_close_matches(a, names, n=5, cutoff=0.6)))[:5]
            raise NotFound("%s has no %r in this Apex.%s" % (done, a, " Close: " + ", ".join(close) if close else ""))
        done += "." + a
    return obj, owner


def _class_members(cls):
    props, methods = [], []
    for n in dir(cls):
        if n.startswith("_") or n == "thisown":
            continue
        try:
            raw = inspect.getattr_static(cls, n)
        except AttributeError:
            continue
        if isinstance(raw, (staticmethod, classmethod)):
            raw = raw.__func__
        if callable(raw):
            methods.append(n)
        elif hasattr(raw, "__get__") or isinstance(raw, property):
            props.append(n)  # property, or another descriptor (the fake apex has some)
    return props, methods


def describe(apex, target, flt=None, limit=200, values=True, budget_s=3.0, ns=None, get_entity=None,
             ref=None, modmap=None):
    obj, owner = resolve(apex, target, ns, get_entity)
    modmap = modmap if modmap is not None else module_names(apex)
    out = {"target": target}
    if flt:
        out["filter"] = flt
    if isinstance(obj, types.ModuleType):
        rec = describe_module(obj)
        out.update(kind="module", runtime_name=obj.__name__)
        if obj is not apex:  # apex.mesh also holds names imported from other modules: list those apart
            again = {}
            for key in ("functions", "classes", "enums"):
                own = []
                for n in rec[key]:
                    home = getattr(getattr(obj, n, None), "__module__", None)
                    if home and home != obj.__name__ and home in modmap:
                        again.setdefault(modmap[home], []).append(n)
                    else:
                        own.append(n)
                rec[key] = own if key != "enums" else {n: rec[key][n] for n in own}
            if again:
                out["reexported"] = {m: len(v) for m, v in sorted(again.items())}
        for key in ("functions", "classes", "modules", "other"):
            names = _match(rec[key], flt)
            if names:
                out[key] = _capped(names, limit)
        enums = _match(rec["enums"], flt)
        if enums:
            out["enums"] = _capped(enums, limit)
        return out
    if isinstance(obj, type):
        members = enum_members(obj)
        out["runtime_name"] = "%s.%s" % (obj.__module__, obj.__name__)
        if members is not None:
            out.update(kind="enum", members={m: int(inspect.getattr_static(obj, m)) for m in members})
            return out
        props, methods = _class_members(obj)
        out.update(kind="class", bases=[c.__name__ for c in obj.__mro__[1:] if c is not object])
        if _match(props, flt):
            out["properties"] = _capped(_match(props, flt), limit)
        if _match(methods, flt):
            out["methods"] = _capped(_match(methods, flt), limit)
        return out
    if obj is None or isinstance(obj, (bool, int, float, str, list, tuple, dict)):
        out.update(kind="value", value=summarize(obj, ref))
        return out
    if isinstance(obj, property) and isinstance(owner, type):
        out.update(kind="property", owner=owner.__name__)
        return out
    if callable(obj) and not hasattr(type(obj), "pathName"):
        is_method = isinstance(owner, type) or (owner is not None and not isinstance(owner, types.ModuleType))
        out.update(kind="method" if is_method else "function",
                   signature=signature(obj, method=isinstance(owner, type)))
        if is_method:
            cls = owner if isinstance(owner, type) else type(owner)
            out["owner"] = cls.__name__
            out["owner_bases"] = [c.__name__ for c in cls.__mro__[1:] if c is not object]
        elif target.startswith("apex.") and isinstance(owner, types.ModuleType):
            out["module"] = target.rsplit(".", 1)[0]  # where it was asked for, not its runtime home ('leaf')
        else:
            out["module"] = modmap.get(getattr(obj, "__module__", "") or "", getattr(obj, "__module__", None))
        doc = (getattr(obj, "__doc__", None) or "").strip()
        if doc:
            out["doc"] = doc[:600]
        return out
    # an instance: an entity or a script variable
    cls = type(obj)
    props, methods = _class_members(cls)
    props += [n for n in getattr(obj, "__dict__", {}) if not n.startswith("_") and n not in ("this", "thisown")
              and n not in props and not callable(getattr(obj, "__dict__", {})[n])]
    out.update(kind="object", cls=cls.__name__, runtime_name="%s.%s" % (cls.__module__, cls.__name__),
               bases=[c.__name__ for c in cls.__mro__[1:] if c is not object])
    if ref is not None:
        try:
            out["ref"] = ref(obj)
        except Exception:
            pass
    shown = _match(props, flt)
    if values:
        vals, skipped, slow, unshown, t0 = {}, [], [], [], time.time()
        slow_names = {n for c in cls.__mro__ if c.__name__ in SLOW_INTERFACES for n in vars(c)}
        for n in sorted(shown):
            if BIG_PROPERTY.search(n):
                skipped.append(n)
                continue
            if n in slow_names:
                slow.append(n)
                continue
            if len(vals) >= limit:
                unshown.append(n)
                continue
            if time.time() - t0 > budget_s:
                vals[n] = "(not read: time budget used up)"
                continue
            try:
                vals[n] = summarize(getattr(obj, n), ref)
            except Exception as exc:
                vals[n] = "(error: %s)" % str(exc)[:120]
        out["properties"] = vals
        if skipped:
            out["not_read"] = skipped
        if slow:
            out["slow"] = slow
        if unshown:
            out["not_shown"] = unshown
    elif shown:
        out["properties"] = _capped(shown, limit)
    if _match(methods, flt):
        out["methods"] = _capped(_match(methods, flt), limit)
    return out


def m_describe(W, p):
    apex = W.ensure_apex()
    return describe(apex, p.get("target") or "apex", flt=p.get("filter"), limit=int(p.get("limit") or 200),
                    values=bool(p.get("values", True)), budget_s=float(p.get("budget_s") or 3.0),
                    ns=W.STATE.get("ns"), get_entity=W.one_entity, ref=W.S.ref)


TOOLS = {"api_index": api_index, "describe": m_describe}
