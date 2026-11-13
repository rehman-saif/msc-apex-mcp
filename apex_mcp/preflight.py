"""Static check of run_apex_python code before it is sent to Apex (runs in the MCP server).

It reads the script with `ast` (nothing is executed) and checks the apex names it uses against
two indexes:
  * live: what the running Apex really has (introspect.api_index, fetched once per session);
  * doc:  the generated API reference of the apex-scripting skill.

Errors stop the script; they are limited to things that are certain to fail:
  * a syntax error;
  * an apex module member that this Apex does not have (live index), or that the runtime
    overrides list as moved or absent;
  * an enum member that this Apex does not have (live index);
  * a keyword argument the function does not take, a missing required argument or too many
    positional ones (runtime signature, or the documented parameters, which match the runtime
    for every function where Apex 2024.1 exposes one);
  * a loop over `.elements` / `.nodes` (it walks every element object; on a 90k-element model
    that exhausted Apex's memory).
Everything less certain is a warning and the script still runs. A use inside `try` with a broad
`except`, or under an `if hasattr(...)`, is only a warning: the script expects it may fail.

mode="query" (query_apex_python) also refuses anything that would change the model or the scene,
write a file or start a process (see Checker.read_only). It guards against mistakes; it is not a
sandbox, so the worker's effects snapshot backs it up.
"""
import ast
import difflib
import json
import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES_PATH = os.path.join(HERE, "runtime_overrides.json")
BIG = ("elements", "nodes")
ITER_FUNCS = {"list", "tuple", "set", "frozenset", "sorted", "enumerate", "sum", "max", "min", "any",
              "all", "zip", "map", "filter", "reversed", "dict"}
BROAD = {"Exception", "BaseException", "AttributeError", "TypeError"}
MAX_LISTED = 30

# ------------------------------------------------------------- read-only (query) mode
# Apex names read by verb: getX, isX, asX, evaluateX, measureX, findX ... (get also before a
# lower-case letter: getuTangent, getisLocked). 'Fix'/'Repair' inside a name means it changes
# geometry (findFixSmallSurfaceAndCurveFeatures, identifyFixSmallFeatures).
READ_NAME = re.compile(r"^(get|(is|has|as|evaluate|measure|find|identify|check|compare|analyse|analyze|current|"
                       r"to|len|index|count|contains|found|this|size|empty)(?![a-z]))")
CHANGES_ANYWAY = re.compile(r"Fix|Repair")
READ_FUNCTIONS = re.compile(r"Collection$|_isValidPrimaryModel$|^SWIG_PyGetDerivedType$")
# Not reads, but allowed in a query: they set how the script itself works, not the model or the view.
QUERY_ALLOWED = {"apex.setScriptUnitSystem"}
# Verbs of methods that change an entity or the scene (camelCase: setName, createMeshBody,
# hide, showOnly). Applied to every method call, Apex or not: other Python objects rarely use them.
CHANGE_VERB = re.compile(
    r"^(set|create|delete|remove|add|update|assign|unassign|import|export|save|apply|unset|show|hide|highlight|"
    r"activate|deactivate|move|insert|associate|disassociate|attach|detach|clear|merge|split|trim|renumber|"
    r"reverse|flip|clone|duplicate|complete|execute|generate|recalculate|enable|disable|open|close|exit|rotate|"
    r"roll|pitch|yaw|dolly|pan|zoom|reset|view|render|capture|suppress|unsuppress|transform|defeature|align|"
    r"drag|push|stitch|unstitch|separate|extend|reparent|undo|redo|begin|end|run|script|terminate|initialize|"
    r"register|edit|change|build|bind|bulkbind|construct|filler|intersect|subtract|grow|reduce|mask|stop|"
    r"cancel|checkin|checkout|importdata|exportdata|project|mesh|fill|copy|paste|rename|lock|unlock)(?![a-z])")
# Methods that only change a Python-side container (list, dict, set, EntityCollection, SWIG
# vectors), or plain Python: allowed although their verb would otherwise count.
CONTAINER_METHODS = {
    "append", "appendList", "extend", "insert", "insertList", "insertCollection", "remove", "pop", "popitem",
    "clear", "add", "discard", "sort", "reverse", "index", "count", "copy", "keys", "values", "items",
    "setdefault", "get", "has_key", "iterator", "size", "empty", "begin", "end", "rbegin", "rend", "erase",
    "resize", "swap", "assign", "front", "back", "capacity", "reserve", "push_back", "pop_back", "asdict",
    "fromList", "toList", "len", "close", "join", "split", "strip", "format", "intersection", "union",
    "difference", "symmetric_difference", "intersection_update", "difference_update", "issubset", "issuperset",
    "most_common"}
# Python calls that write files or run code the check cannot see.
WRITE_METHODS = {"write_text", "write_bytes", "unlink", "mkdir", "rmdir", "touch", "symlink_to", "hardlink_to",
                 "chmod", "save", "savetxt", "savez", "savez_compressed", "tofile", "to_csv", "to_excel",
                 "to_parquet", "to_pickle", "to_hdf", "savefig"}  # file.write needs open(.., 'w'): caught there
UNCHECKABLE_BUILTINS = {"exec", "eval", "compile", "__import__", "setattr", "delattr", "breakpoint", "input"}
UNSAFE_MODULES = {"subprocess", "shutil", "socket", "ctypes", "multiprocessing", "winreg", "_winapi", "importlib",
                  "runpy", "urllib", "http", "requests", "smtplib", "ftplib", "webbrowser", "signal", "threading",
                  "concurrent", "asyncio", "tempfile", "pickle", "shelve", "sqlite3", "zipfile", "tarfile"}
OS_WRITES = {"system", "popen", "remove", "unlink", "rename", "renames", "replace", "rmdir", "removedirs", "mkdir",
             "makedirs", "startfile", "kill", "chmod", "chdir", "putenv", "unsetenv", "truncate", "write", "symlink",
             "link", "utime", "spawnl", "spawnv", "execv", "execl", "_exit", "abort"}
PY_DICT_CALLS = {"dict", "set", "frozenset", "defaultdict", "OrderedDict", "Counter", "ChainMap"}
QUERY_HINT = "query_apex_python only reads; use run_apex_python for this"


def load_overrides(path=OVERRIDES_PATH):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class Index:
    """Live names when available, the documented ones otherwise."""

    def __init__(self, doc=None, live=None, overrides=None):
        self.doc = doc or {"modules": set(), "functions": {}, "classes": set(), "enums": {}}
        self.live = live.get("modules") if live else None
        self.ov = overrides or {}
        self.doc_members = {}
        for kind, names in (("function", self.doc["functions"]), ("class", self.doc["classes"]),
                            ("enum", self.doc["enums"])):
            for q in names:
                mod, name = q.rsplit(".", 1)
                self.doc_members.setdefault(mod, {})[name] = kind
        self.doc_modules = set(self.doc["modules"]) | {"apex"}
        # method names of Apex classes (documented, plus the live ones when the index has them)
        self.methods = set(self.doc.get("methods") or ()) | set((live or {}).get("method_names") or ())

    @property
    def has_live(self):
        return self.live is not None

    def is_module(self, q):
        return q in self.live if self.has_live else q in self.doc_modules

    def kind(self, q):
        mod, name = q.rsplit(".", 1)
        if self.has_live:
            m = self.live.get(mod)
            if m is None:
                return None
            for kind, key in (("function", "functions"), ("enum", "enums"), ("class", "classes"),
                              ("module", "modules"), ("other", "other")):
                if name in m.get(key, ()):
                    return kind
            return None
        return self.doc_members.get(mod, {}).get(name)

    def names(self, mod):
        if self.has_live and mod in self.live:
            m = self.live[mod]
            return set(m["functions"]) | set(m["classes"]) | set(m["enums"]) | set(m["modules"]) | set(m["other"])
        return set(self.doc_members.get(mod, {}))

    def elsewhere(self, name, skip):
        """Other modules that have this name: 'apex.session.ConstraintAxis' for apex.ConstraintAxis."""
        mods = self.live if self.has_live else {m: None for m in self.doc_modules}
        return sorted(m + "." + name for m in mods if m != skip and name in self.names(m)
                      and not m.endswith("_plugin") and m not in ("apex.glef", "apex.leaf", "apex.clef"))

    def live_params(self, q):
        if not self.has_live:
            return None
        mod, name = q.rsplit(".", 1)
        return (self.live.get(mod) or {}).get("functions", {}).get(name)

    def doc_params(self, q):
        return self.doc["functions"].get(q)

    def enum_members(self, q):
        if self.has_live:
            mod, name = q.rsplit(".", 1)
            m = (self.live.get(mod) or {}).get("enums", {})
            if name in m:
                return m[name], "live"
        if q in self.doc["enums"]:
            return self.doc["enums"][q], "doc"
        return None, None


def _chain(node):
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None, None
    return node.id, out[::-1]


def _close(word, candidates, n=3):
    return difflib.get_close_matches(word, sorted(candidates), n=n, cutoff=0.6)


def _listing(names):
    names = list(names)
    head = ", ".join(names[:MAX_LISTED])
    return head + (", ... (%d in all)" % len(names) if len(names) > MAX_LISTED else "")


class Checker:
    def __init__(self, code, index, mode="run"):
        self.code, self.ix, self.mode = code, index, mode
        self.errors, self.warnings = [], []
        self.parents = {}
        self.alias = {}
        self.defined = set()  # names the script defines itself (functions, classes)
        self.reported = set()
        self.bindings, self.assigned = Counter(), {}  # name -> times bound, values of its plain assignments

    # ------------------------------------------------------------------ report
    def _add(self, node, msg, error=True, name=None):
        line = getattr(node, "lineno", None)
        rec = {"line": line, "msg": msg}
        if name:
            rec["name"] = name
        if error and self.guarded(node):
            rec["msg"] += " (inside try/except or hasattr, so not blocking)"
            error = False
        target = self.errors if error else self.warnings
        if rec not in target:
            target.append(rec)

    def guarded(self, node):
        child, cur = node, self.parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try) and child in cur.body:
                for h in cur.handlers:
                    if h.type is None:
                        return True
                    types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
                    if any(isinstance(t, ast.Name) and t.id in BROAD for t in types):
                        return True
            if isinstance(cur, (ast.If, ast.IfExp)) and child is not cur.test:
                if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("hasattr", "getattr")
                       for n in ast.walk(cur.test)):
                    return True
            child, cur = cur, self.parents.get(cur)
        return False

    # ----------------------------------------------------------------- aliases
    def collect(self, tree):
        bindings, cand = Counter(), {}
        for node in ast.walk(tree):
            for ch in ast.iter_child_nodes(node):
                self.parents[ch] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bindings[node.id] += 1
                par = self.parents.get(node)
                if isinstance(par, ast.Assign) and len(par.targets) == 1 and par.targets[0] is node:
                    cand.setdefault(node.id, []).append(("expr", par.value))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings[node.name] += 1
                self.defined.add(node.name)
            elif isinstance(node, ast.arg):
                bindings[node.arg] += 1
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bindings[node.name] += 1
            elif isinstance(node, ast.Import):
                for a in node.names:
                    bound = a.asname or a.name.split(".")[0]
                    bindings[bound] += 1
                    if a.name == "apex" or a.name.startswith("apex."):
                        cand.setdefault(bound, []).append(("chain", a.name.split(".") if a.asname else ["apex"]))
            elif isinstance(node, ast.ImportFrom):
                apexy = node.level == 0 and node.module and (node.module == "apex" or node.module.startswith("apex."))
                for a in node.names:
                    if a.name == "*":
                        continue
                    bound = a.asname or a.name
                    bindings[bound] += 1
                    if apexy:
                        cand.setdefault(bound, []).append(("chain", node.module.split(".") + [a.name]))
            elif hasattr(ast, "MatchAs") and isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                bindings[node.name] += 1
        self.bindings = bindings
        self.assigned = {n: [c for kind, c in cs if kind == "expr"] for n, cs in cand.items()}
        if bindings["apex"] == 0:
            self.alias["apex"] = ["apex"]  # run_apex_python puts apex in the namespace
        for _ in range(4):  # m = apex.mesh; S = m.SurfaceMeshElementShape: resolve in a few passes
            for name, cs in cand.items():
                if len(cs) != bindings[name]:
                    continue
                chains = [c if kind == "chain" else self.chain_of(c) for kind, c in cs]
                if all(chains) and all(c == chains[0] for c in chains):
                    self.alias[name] = chains[0]

    def chain_of(self, node):
        if isinstance(node, ast.Name):
            return list(self.alias[node.id]) if node.id in self.alias else None
        base, attrs = _chain(node)
        if base is None or base not in self.alias:
            return None
        return self.alias[base] + attrs

    # ------------------------------------------------------------------- names
    def resolve(self, chain, node):
        """(kind, qualified name) of an apex chain; reports names this Apex does not have."""
        ix = self.ix
        if any(c.startswith("__") for c in chain):  # apex.__name__, Enum.__mro__: Python's, not Apex's
            chain = chain[:next(i for i, c in enumerate(chain) if c.startswith("__"))]
        mod, i = "apex", 1
        while i < len(chain) and ix.is_module(mod + "." + chain[i]):
            mod += "." + chain[i]
            i += 1
        if i >= len(chain):
            return "module", mod
        q = mod + "." + chain[i]
        kind = ix.kind(q)
        if kind is None or q in (ix.ov.get("moved") or {}) or q in (ix.ov.get("absent") or []):
            self.missing(q, node)
            return None, q
        if kind == "enum" and i + 1 < len(chain):
            self.enum_member(q, chain[i + 1], node)
        return kind, q

    def missing(self, q, node):
        if q in self.reported:  # once per name: the import and every use would repeat it
            return
        self.reported.add(q)
        ov = self.ix.ov
        mod, name = q.rsplit(".", 1)
        moved = (ov.get("moved") or {}).get(q)
        if moved:
            return self._add(node, "%s is %s in this Apex (the API reference has it in the wrong module)" % (q, moved), name=q)
        if q in (ov.get("absent") or []):
            return self._add(node, "%s is in the API reference but not in this Apex" % q, name=q)
        hint = _close(name, self.ix.names(mod))
        other = self.ix.elsewhere(name, mod)
        text = ""
        if other:
            text += " It exists as %s." % ", ".join(other[:3])
        if hint:
            text += " Did you mean %s?" % ", ".join(mod + "." + h for h in hint)
        if self.ix.has_live:
            self._add(node, "%s does not exist in this Apex.%s" % (q, text), name=q)
        else:
            self._add(node, "%s is not in the API reference (it may be undocumented).%s" % (q, text), error=False, name=q)

    def enum_member(self, q, member, node):
        members, source = self.ix.enum_members(q)
        if members is None or member in members:
            return
        near = _close(member, members)
        msg = "%s has no member %r.%s Members: %s" % (q, member, (" Did you mean %s?" % ", ".join(near)) if near else "",
                                                     _listing(members))
        self._add(node, msg if source == "live" else msg + " (per the API reference)", error=(source == "live"), name=q)

    def call(self, q, node):
        kws = [k.arg for k in node.keywords]
        notes = (self.ix.ov.get("notes") or {}).get(q)
        if notes:
            self._add(node, "%s: %s" % (q, notes), error=False, name=q)
        if None in kws:  # f(**opts): the names are not known here
            return
        live = self.ix.live_params(q)
        if live is not None:
            names = [n for n, _ in live]
            self.unknown_kwargs(q, kws, [names], node, "")
            if any(isinstance(a, ast.Starred) for a in node.args):
                return
            npos = len(node.args)
            if npos > len(names):
                self._add(node, "%s() takes %d argument(s) (%s), %d given" % (q, len(names), ", ".join(names), npos), name=q)
            missing = [n for i, (n, req) in enumerate(live) if req and i >= npos and n not in kws]
            if missing:
                self._add(node, "%s() is missing required argument(s): %s" % (q, ", ".join(missing)), name=q)
            return
        doc = self.ix.doc_params(q)
        if doc:
            self.unknown_kwargs(q, kws, doc, node, " (documented arguments)")

    def unknown_kwargs(self, q, kws, overloads, node, where):
        allowed = {n for o in overloads for n in o}
        bad = [k for k in kws if k not in allowed]
        for k in bad:
            near = _close(k, allowed)
            self._add(node, "%s() has no argument %r.%s Arguments%s: %s"
                      % (q, k, (" Did you mean %s?" % ", ".join(near)) if near else "", where,
                         " | ".join(_listing(o) or "(none)" for o in overloads)), name=q)

    # ---------------------------------------------------------------- patterns
    def big_iteration(self, tree):
        def is_big(expr):
            return isinstance(expr, ast.Attribute) and expr.attr in BIG
        for node in ast.walk(tree):
            target = None
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and is_big(node.iter):
                target = node.iter
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ITER_FUNCS
                  and node.args and is_big(node.args[0])):
                target = node.args[0]
            if target is not None:
                src = ast.get_source_segment(self.code, target) or "." + target.attr
                self._add(target, "iterates %s: that walks every %s object, and on a large mesh it can exhaust "
                                  "Apex's memory (it crashed Apex on a 90k-element model). Use the bulk arrays "
                                  "(mesh.getElementIDs(), getNodeIDs(), getNodeCoordinates(), "
                                  "getElementConnectivities()) or the Q helpers. If this collection is known "
                                  "to be small, resend with force=True." % (src, target.attr[:-1]))

    def absent_methods(self, tree):
        absent = self.ix.ov.get("absent_methods") or {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in absent
                    and node.func.attr not in self.defined and self.chain_of(node.func) is None):
                self._add(node, "%s is in the API reference but not in Apex 2024.1" % absent[node.func.attr],
                          error=False)

    # --------------------------------------------------------------- read-only
    def refuse(self, node, msg):
        """A query-mode error: never softened by try/except, the call would still run."""
        rec = {"line": getattr(node, "lineno", None), "msg": "%s: %s." % (msg, QUERY_HINT), "query": True}
        if rec not in self.errors:
            self.errors.append(rec)

    def query_function(self, q, node, call=None):
        name = q.rsplit(".", 1)[1]
        if READ_NAME.match(name) and CHANGES_ANYWAY.search(name):
            # findFix.../identifyFix... only detect with a literal autoFix=False (geometry_check does that)
            fix = next((k.value for k in getattr(call, "keywords", ()) if k.arg == "autoFix"), None)
            if isinstance(fix, ast.Constant) and fix.value is False:
                return
            return self.refuse(node, "%s repairs geometry unless called with autoFix=False" % q)
        if READ_NAME.match(name) or READ_FUNCTIONS.search(name) or q in QUERY_ALLOWED:
            return
        self.refuse(node, "%s changes the model, the scene or the session" % q)

    def python_typed(self, node, depth=0):
        """True when node is certainly a Python dict/set/list (so .update() is not an entity's)."""
        if isinstance(node, (ast.Dict, ast.DictComp, ast.Set, ast.SetComp, ast.List, ast.ListComp)):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            return (isinstance(f, ast.Name) and f.id in PY_DICT_CALLS) or \
                   (isinstance(f, ast.Attribute) and f.attr in PY_DICT_CALLS)
        if isinstance(node, ast.Name) and depth < 4:
            vals = self.assigned.get(node.id) or []
            return bool(vals) and len(vals) == self.bindings[node.id] and \
                all(self.python_typed(v, depth + 1) for v in vals)
        return False

    def read_only(self, tree):
        modalias = {"np": "numpy", "Q": "queries", "S": "serialize"}  # bound name -> module (not apex)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in UNSAFE_MODULES:
                        self.refuse(node, "import %s can start processes, write files or run code the check cannot "
                                          "see" % a.name)
                    if not (a.name == "apex" or a.name.startswith("apex.")):
                        modalias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                root = node.module.split(".")[0]
                if root in UNSAFE_MODULES:
                    self.refuse(node, "from %s import ... can start processes, write files or run code the check "
                                      "cannot see" % node.module)
                elif root == "os":
                    for a in node.names:
                        if a.name in OS_WRITES:
                            self.refuse(node, "os.%s writes files or starts processes" % a.name)
                if root != "apex":  # from PIL import Image: Image.open is a module's, not an entity's
                    for a in node.names:
                        if a.name != "*":
                            modalias[a.asname or a.name] = node.module + "." + a.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name):
                if f.id in self.defined:
                    continue
                if f.id in UNCHECKABLE_BUILTINS:
                    self.refuse(node, "%s() runs or changes code the check cannot see" % f.id)
                elif f.id == "open":
                    self.open_mode(node)
                elif f.id == "getattr" and len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant) \
                        and isinstance(self.parents.get(node), ast.Call) and self.parents[node].func is node:
                    self._add(node, "getattr(..., <name>)(...) calls a method chosen at run time; the read-only "
                                    "check cannot see which", error=False)
                continue
            if not isinstance(f, ast.Attribute):
                continue
            name = f.attr
            if self.chain_of(f) is not None:
                continue  # apex.x.y(...): checked against the apex index (query_function)
            base, attrs = _chain(f)
            mod = modalias.get(base) if base is not None and base not in self.assigned else None
            if mod is not None:  # a Python module's function: np.delete, os.remove, json.dumps
                full = ".".join([mod] + attrs)
                if mod == "os" and len(attrs) == 1 and name in OS_WRITES:
                    self.refuse(node, "os.%s writes files or starts processes" % name)
                elif full in ("io.open", "codecs.open"):
                    self.open_mode(node, 1 if full == "io.open" else 1)
                elif name in WRITE_METHODS:
                    self.refuse(node, "%s() writes a file" % full)
                continue
            if name in WRITE_METHODS:
                self.refuse(node, ".%s() writes a file" % name)
            elif name == "update":
                if not self.python_typed(f.value):
                    src = ast.get_source_segment(self.code, f.value) or "it"
                    hint = (" If %s is a Python dict, build it in this script with {...} or dict(), or merge "
                            "with %s |= {...}" % (src, src)) if isinstance(f.value, ast.Name) else ""
                    self.refuse(node, "%s.update(...) changes an Apex entity (Part.update and the like).%s"
                                % (src, hint))
            elif name in CONTAINER_METHODS or READ_NAME.match(name):
                continue
            elif CHANGE_VERB.match(name) or (name in self.ix.methods and name != name.lower()):
                # an Apex method name (camelCase: Python's own methods are lower case) that is not a read
                self.refuse(node, ".%s() changes the model or the scene" % name)

    def open_mode(self, node, pos=1):
        mode = node.args[pos] if len(node.args) > pos else next((k.value for k in node.keywords if k.arg == "mode"), None)
        if mode is None:
            return  # 'r'
        if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            self.refuse(node, "open() with a mode set at run time may write a file (give 'r' or 'rb')")
        elif set(mode.value) & set("wax+"):
            self.refuse(node, "open(..., %r) writes a file" % mode.value)

    # -------------------------------------------------------------------- main
    def run(self):
        try:
            tree = ast.parse(self.code)
        except SyntaxError as exc:
            self.errors.append({"line": exc.lineno, "msg": "SyntaxError: %s" % exc.msg})
            return
        self.collect(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                    (node.module == "apex" or node.module.startswith("apex.")):
                for a in node.names:
                    if a.name != "*":
                        kind, q = self.resolve(node.module.split(".") + [a.name], node)
                        if kind == "function" and self.mode == "query":
                            self.query_function(q, node)
                continue
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                par = self.parents.get(node)
                if isinstance(par, ast.Attribute) and par.value is node:
                    continue  # only whole chains: apex.mesh.X, not apex.mesh inside it
                chain = self.chain_of(node)
                if not chain or len(chain) < 2:
                    continue
                kind, q = self.resolve(chain, node)
                if kind == "function" and self.mode == "query":  # referenced, not only called: f = apex.deleteEntities
                    self.query_function(q, node, par if isinstance(par, ast.Call) and par.func is node else None)
                if kind == "function" and isinstance(par, ast.Call) and par.func is node:
                    self.call(q, par)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.alias:
                chain = self.alias[node.func.id]
                if len(chain) >= 2:
                    kind, q = self.resolve(chain, node)
                    if kind == "function":
                        self.call(q, node)
        self.big_iteration(tree)
        self.absent_methods(tree)
        if self.mode == "query":
            self.read_only(tree)
        for lst in (self.errors, self.warnings):
            lst.sort(key=lambda r: (r["line"] or 0, r["msg"]))


def check(code, doc=None, live=None, overrides=None, mode="run"):
    """{'errors': [{line, msg, name?}], 'warnings': [...]} for a script. mode='query' adds the
    read-only checks of query_apex_python."""
    c = Checker(code, Index(doc, live, overrides if overrides is not None else load_overrides()), mode)
    c.run()
    return {"errors": c.errors, "warnings": c.warnings}


def format_issues(items):
    return "\n".join("  line %s: %s" % (r["line"] if r["line"] is not None else "?", r["msg"]) for r in items)
