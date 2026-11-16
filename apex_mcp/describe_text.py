"""Text for apex_describe and for the failing call of a script: what the running Apex has
(worker/introspect). Apex's objects carry no docstrings, so descriptions and per-argument
documentation are not here: they are in the apex-scripting skill (nastran-fea-skills)."""

SKILL_HINT = "The apex-scripting skill documents its arguments (api/modules/%s.md)."


def runtime_sig(name, sig):
    if not sig or sig.get("unknown"):
        return "%s(?)  (Apex does not expose a signature)" % name
    if sig.get("star"):
        return "%s(*args, **kwargs)  (Apex does not expose the argument names)" % name
    parts = []
    for p in sig.get("params", []):
        s = p["name"] + (": " + p["type"] if p.get("type") else "")
        if "default" in p:
            s += " = " + p["default"]
        parts.append(s)
    return "%s(%s)%s" % (name, ", ".join(parts), " -> " + sig["returns"] if sig.get("returns") else "")


def call_line(rec):
    """One line for a failing apex call from the worker's record: its signature in this Apex, and
    where the documentation is when Apex does not expose the argument names."""
    sig = rec.get("signature")
    text = runtime_sig(rec["name"], sig)
    if not sig or sig.get("star") or sig.get("unknown"):
        text += " " + SKILL_HINT % (rec.get("module") if rec.get("kind") == "method" else rec["name"].rsplit(".", 1)[0])
    return text


def _names_line(label, rec, width=110):
    """'functions (12): a, b, c' wrapped; rec = {'names', 'total'?}."""
    names = rec["names"]
    head = "%s (%d%s):" % (label, rec.get("total", len(names)),
                           ", first %d shown" % len(names) if rec.get("total") else "")
    lines, cur = [head], " "
    for m in names:
        if len(cur) + len(m) + 2 > width:
            lines.append(cur.rstrip(", "))
            cur = " "
        cur += " " + m + ","
    if cur.strip():
        lines.append(cur.rstrip(","))
    return lines


def _value(v):
    if isinstance(v, dict) and "type" in v and "count" in v:
        extra = ""
        if "values" in v:
            extra = " " + str(v["values"])
        elif "keys" in v:
            extra = " keys " + ", ".join(v["keys"])
        return "%s (%d)%s" % (v["type"], v["count"], extra)
    if isinstance(v, dict) and "path" in v:
        return "%s %r" % (v.get("type"), v["path"])
    if isinstance(v, dict) and "type" in v:
        rest = {k: x for k, x in v.items() if k != "type"}
        return "%s %s" % (v["type"], rest) if rest else v["type"]
    if isinstance(v, str) and not v.startswith("("):
        return repr(v)
    return str(v)


def format_describe(res):
    target, kind = res.get("target"), res.get("kind")
    out = []
    if kind == "module":
        out.append("%s  (module; runtime name %s)" % (target, res.get("runtime_name")))
        for key, label in (("functions", "functions"), ("classes", "classes"), ("enums", "enums"),
                           ("modules", "submodules"), ("other", "other")):
            if key in res:
                out += _names_line(label, res[key])
        if res.get("reexported"):
            out.append("also importable from here (defined in another module, describe them there): " +
                       ", ".join("%d from %s" % (n, m) for m, n in res["reexported"].items()))
        out.append("Describe a member for its signature or values, e.g. apex_describe('%s.<name>')." % target)
    elif kind in ("function", "method"):
        name = target.rsplit(".", 1)[-1]
        if kind == "function":
            qual = "%s.%s" % (res.get("module"), name)
            out.append("%s  (function)" % qual)
            out.append(runtime_sig(qual, res.get("signature")))
        else:
            out.append("%s.%s  (method of %s)" % (res.get("owner"), name, res.get("owner")))
            out.append(runtime_sig("%s.%s" % (res.get("owner"), name), res.get("signature")))
        if res.get("doc"):
            out.append("doc: " + res["doc"])
    elif kind == "enum":
        out.append("%s  (enum; runtime name %s)" % (target, res.get("runtime_name")))
        out.append("members: " + ", ".join("%s=%s" % (k, v) for k, v in res.get("members", {}).items()))
    elif kind == "class":
        out.append("%s  (class; runtime name %s; bases %s)" % (target, res.get("runtime_name"),
                                                                 ", ".join(res.get("bases", [])) or "-"))
        if "properties" in res:
            out += _names_line("properties", res["properties"])
        if "methods" in res:
            out += _names_line("methods", res["methods"])
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
            out += _names_line("methods", res["methods"])
        out.append("Change properties with .update(...); plain assignment raises AttributeError.")
    else:
        out.append(str(res))
    return "\n".join(out)
