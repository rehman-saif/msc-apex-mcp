"""Text for apex_describe: what the running Apex has (worker/introspect.describe), merged with the
generated API reference (signatures and descriptions, which Apex itself does not carry: its
objects have no docstrings) and the runtime overrides."""

UNDOC = "*"
ELSEWHERE = "^"


def runtime_sig(name, sig):
    if not sig or sig.get("unknown"):
        return "%s(?)  (Apex does not expose a signature)" % name
    if sig.get("star"):
        return "%s(*args, **kwargs)  (Apex does not expose the argument names; the reference's are below)" % name
    parts = []
    for p in sig.get("params", []):
        s = p["name"] + (": " + p["type"] if p.get("type") else "")
        if "default" in p:
            s += " = " + p["default"]
        parts.append(s)
    return "%s(%s)%s" % (name, ", ".join(parts), " -> " + sig["returns"] if sig.get("returns") else "")


def _names_line(label, rec, documented=None, elsewhere=(), width=110):
    """'functions (12, * = not in the API reference): a, b*, c' wrapped; rec = {'names', 'total'?}.
    A name the reference documents only under another module is marked ^ instead of *."""
    names = rec["names"]

    def mark(n):
        if documented is None or n in documented:
            return n
        return n + (ELSEWHERE if n in elsewhere else UNDOC)
    marked = [mark(n) for n in names]
    legend = [text for sym, text in ((UNDOC, "%s = not in the API reference" % UNDOC),
                                     (ELSEWHERE, "%s = documented under another module" % ELSEWHERE))
              if any(m.endswith(sym) for m in marked)]
    head = "%s (%d%s%s):" % (label, rec.get("total", len(names)),
                             ", first %d shown" % len(names) if rec.get("total") else "",
                             "".join(", " + t for t in legend))
    lines, cur = [head], " "
    for m in marked:
        if len(cur) + len(m) + 2 > width:
            lines.append(cur.rstrip(", "))
            cur = " "
        cur += " " + m + ","
    if cur.strip():
        lines.append(cur.rstrip(","))
    return lines


def _module_docs(doc, module):
    out = {}
    for key in ("functions", "classes", "enums"):
        names = doc.get(key) or {}
        out[key] = {q.rsplit(".", 1)[1] for q in names if q.rsplit(".", 1)[0] == module}
    return out


def _value(v):
    if isinstance(v, dict) and "type" in v and "count" in v:
        extra = ""
        if "values" in v:
            extra = " " + str(v["values"])
        elif "keys" in v:
            extra = " keys " + ", ".join(v["keys"])
        return "%s (%d)%s" % (v["type"], v["count"], extra)
    if isinstance(v, dict) and "path" in v:
        return "%s %r" % (v.get("class") or v.get("type"), v["path"])
    if isinstance(v, dict) and "type" in v:
        rest = {k: x for k, x in v.items() if k != "type"}
        return "%s %s" % (v["type"], rest) if rest else v["type"]
    if isinstance(v, str) and not v.startswith("("):
        return repr(v)
    return str(v)


def format_describe(res, sk=None, overrides=None):
    doc = sk.doc_index() if sk is not None else {}
    ov = overrides or {}
    target, kind = res.get("target"), res.get("kind")
    out = []

    def note(qual):
        if qual in (ov.get("moved") or {}):
            out.append("note: the API reference has this at %s; in this Apex it is %s" % (qual, ov["moved"][qual]))
        if qual in (ov.get("notes") or {}):
            out.append("note: " + ov["notes"][qual])

    if kind == "module":
        out.append("%s  (module; runtime name %s)" % (target, res.get("runtime_name")))
        docs = _module_docs(doc, target) if sk is not None else None
        for key, label, dkey in (("functions", "functions", "functions"), ("classes", "classes", "classes"),
                                 ("enums", "enums", "enums"), ("modules", "submodules", None), ("other", "other", None)):
            if key in res:
                elsewhere = ({q.rsplit(".", 1)[1] for q in (doc.get(dkey) or {}) if q.rsplit(".", 1)[0] != target}
                             if docs is not None and dkey else ())
                out += _names_line(label, res[key], docs[dkey] if docs is not None and dkey else None, elsewhere)
        if docs is not None:
            have = set()
            for key in ("functions", "classes", "enums"):
                have |= set(res.get(key, {}).get("names", []))
            if not any(res.get(k, {}).get("total") for k in ("functions", "classes", "enums")):
                missing = sorted(n for k in ("functions", "classes", "enums") for n in docs[k] if n not in have)
                flt = res.get("filter")
                if flt:
                    missing = [n for n in missing if flt.lower() in n.lower()]
                if missing:
                    out.append("in the API reference but not in this module at runtime: " + ", ".join(missing[:40]))
        if res.get("reexported"):
            out.append("also importable from here (defined in another module, describe them there): " +
                       ", ".join("%d from %s" % (n, m) for m, n in res["reexported"].items()))
        out.append("Describe a member for its signature or values, e.g. apex_describe('%s.<name>')." % target)
    elif kind in ("function", "method"):
        name = target.rsplit(".", 1)[-1]
        if kind == "function":
            qual = "%s.%s" % (res.get("module"), name)
            out.append("%s  (function)" % qual)
            out.append("runtime:   " + runtime_sig(qual, res.get("signature")))
            sigs = sk.signature(qual) if sk is not None else []
        else:
            out.append("%s.%s  (method of %s)" % (res.get("owner"), name, res.get("owner")))
            out.append("runtime:   " + runtime_sig("%s.%s" % (res.get("owner"), name), res.get("signature")))
            sigs = sk.method_signature(res.get("owner"), name, res.get("owner_bases", ())) if sk is not None else []
            qual = None
        if sk is not None:
            out += ["reference: " + s for s in sigs] or ["reference: not documented"]
        if res.get("doc"):
            out.append("doc: " + res["doc"])
        if qual:
            note(qual)
        if sigs and kind == "function":
            out.append("apex_api_lookup('%s') gives the per-argument documentation." % qual)
    elif kind == "enum":
        members = res.get("members", {})
        documented = set(doc.get("enums", {}).get(target, [])) if target.startswith("apex.") else set()
        out.append("%s  (enum; runtime name %s)" % (target, res.get("runtime_name")))
        out.append("members: " + ", ".join("%s=%s%s" % (k, v, UNDOC if documented and k not in documented else "")
                                           for k, v in members.items()))
        if documented and any(k not in documented for k in members):
            out.append("%s = not in the API reference" % UNDOC)
        gone = sorted(documented - set(members))
        if gone:
            out.append("in the API reference but not in this Apex: " + ", ".join(gone))
        note(target)
    elif kind == "class":
        cls = target.rsplit(".", 1)[-1]
        brief = sk.class_brief(cls) if sk is not None else None
        out.append("%s  (class; runtime name %s; bases %s)" % (target, res.get("runtime_name"),
                                                                 ", ".join(res.get("bases", [])) or "-"))
        if brief and brief[1]:
            out.append(brief[1])
        if "properties" in res:
            out += _names_line("properties", res["properties"])
        if "methods" in res:
            out += _names_line("methods", res["methods"],
                               sk.documented_methods(cls, res.get("bases", ())) if sk is not None else None)
        note(target)
    elif kind == "value":
        out.append("%s = %s" % (target, _value(res.get("value"))))
    elif kind == "property":
        out.append("%s  (property of %s). Describe an instance (an entity pathName, or $variable from "
                   "run_apex_python) to read its value." % (target, res.get("owner")))
    elif kind == "object":
        ref = res.get("ref") or {}
        out.append("%s  (%s; runtime class %s; bases %s)" % (ref.get("path") or target, res.get("cls"),
                                                            res.get("runtime_name"), ", ".join(res.get("bases", [])) or "-"))
        props = res.get("properties")
        if isinstance(props, dict) and "names" not in props:
            out.append("properties:")
            out += ["  %s = %s" % (k, _value(v)) for k, v in props.items()]
        elif props:
            out += _names_line("properties", props)
        if res.get("not_shown"):
            out.append("not shown (limit reached; raise limit or use filter): " + ", ".join(res["not_shown"]))
        if res.get("not_read"):
            out.append("not read (they can hold every element or node of the model; use the bulk getters "
                       "getElementIDs / getNodeIDs / getNodeCoordinates or the Q helpers): " + ", ".join(res["not_read"]))
        if res.get("slow"):
            out.append("not read (computed on every read, about 0.25 s each; describe one directly, e.g. '%s.%s'): %s"
                       % (target if target.startswith("$") else "$<var>", res["slow"][0], ", ".join(res["slow"])))
        if "methods" in res:
            out += _names_line("methods", res["methods"],
                               sk.documented_methods(res.get("cls"), res.get("bases", ())) if sk is not None else None)
        out.append("Change properties with .update(...); plain assignment raises AttributeError.")
    else:
        out.append(str(res))
    return "\n".join(out)
