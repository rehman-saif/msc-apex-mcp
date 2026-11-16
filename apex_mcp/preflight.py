"""Static check of a script before it is sent to Apex (runs in the MCP server; nothing is executed).

Both script tools:
  * a syntax error;
  * a loop over `.elements` / `.nodes`: it makes one Python object per element or node, and on a
    90k-element model that exhausted Apex's memory. force=True runs it anyway.

Unknown apex names and wrong arguments are not checked here. Apex reports them itself, at the
call, and the worker adds the failing call's signature. Measured on 316 saved scripts: checking
names and arguments against the running Apex's API caught 4 of the 39 that failed and refused 2
that ran.

query_apex_python (mode="query") also refuses a script that would change the model or the scene.
Apex names carry their effect in the verb (getX, isX, findX ... read; createX, setX, deleteX,
update, hide ... change), so the rule is by name:
  * a call of an apex function that is not a read;
  * a method call with a changing verb, `.update(...)` on anything but a dict the script builds
    itself, and the model-building helpers `B.*`.
It guards against mistakes; it is not a sandbox (Python in Apex can do anything Apex can, and the
check does not look at files or processes). The worker snapshots the model before and after a
query and the server says so if it changed.
"""
import ast
import re
from collections import Counter

BIG = ("elements", "nodes")
ITER_FUNCS = {"list", "tuple", "set", "frozenset", "sorted", "enumerate", "sum", "max", "min", "any",
              "all", "zip", "map", "filter", "reversed", "dict"}

# Reads by verb: getX, isX, asX, evaluateX, measureX, findX ... (get also before a lower-case
# letter: getuTangent, getisLocked). 'Fix'/'Repair' inside a name means it changes geometry
# (findFixSmallSurfaceAndCurveFeatures, identifyFixSmallFeatures) unless called with autoFix=False.
READ_NAME = re.compile(r"^(get|(is|has|as|evaluate|measure|find|identify|check|compare|analyse|analyze|current|"
                       r"to|len|index|count|contains|found|this|size|empty)(?![a-z]))")
CHANGES_ANYWAY = re.compile(r"Fix|Repair")
READ_FUNCTIONS = re.compile(r"Collection$")  # apex.partCollection() and the like: constructors
# Not reads, but allowed in a query: they set how the script itself works, not the model or the view.
QUERY_ALLOWED = {"apex.setScriptUnitSystem"}
# Verbs of methods that change an entity or the scene (camelCase: setName, createMeshBody, hide,
# showOnly). Applied to every method call, Apex or not: other Python objects rarely use them.
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
PY_DICT_CALLS = {"dict", "set", "frozenset", "defaultdict", "OrderedDict", "Counter", "ChainMap"}
B_READS = {"targets"}  # the only model-building helper that changes nothing
QUERY_HINT = "query_apex_python only reads; use run_apex_python for this"


def _chain(node):
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None, None
    return node.id, out[::-1]


class Checker:
    def __init__(self, code, mode="run"):
        self.code, self.mode = code, mode
        self.errors = []
        self.parents = {}
        self.alias = {}       # name -> apex chain it stands for: m = apex.mesh -> ['apex', 'mesh']
        self.bindings, self.assigned = Counter(), {}  # name -> times bound, values of its plain assignments

    def _add(self, node, msg, query=False):
        rec = {"line": getattr(node, "lineno", None), "msg": msg}
        if query:  # a read-only refusal: force cannot override it
            rec["query"] = True
        if rec not in self.errors:
            self.errors.append(rec)

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
            self.alias["apex"] = ["apex"]  # the script tools put apex in the namespace
        for _ in range(4):  # m = apex.mesh; f = m.createSolidMesh: resolve in a few passes
            for name, cs in cand.items():
                if len(cs) != bindings[name]:
                    continue  # the name means more than one thing: not followed
                chains = [c if kind == "chain" else self.chain_of(c) for kind, c in cs]
                if all(chains) and all(c == chains[0] for c in chains):
                    self.alias[name] = chains[0]

    def chain_of(self, node):
        """['apex', 'mesh', 'createSolidMesh'] if node is an apex name (also through aliases), else None."""
        if isinstance(node, ast.Name):
            return list(self.alias[node.id]) if node.id in self.alias else None
        base, attrs = _chain(node)
        if base is None or base not in self.alias:
            return None
        return self.alias[base] + attrs

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

    # --------------------------------------------------------------- read-only
    def refuse(self, node, msg):
        self._add(node, "%s: %s." % (msg, QUERY_HINT), query=True)

    def apex_call(self, chain, node):
        """A call of apex.<...>.<name>(...): a class or enum (capitalised) and a read are fine."""
        name, q = chain[-1], ".".join(chain)
        if len(chain) < 2 or name[:1].isupper() or q in QUERY_ALLOWED or READ_FUNCTIONS.search(name):
            return
        if READ_NAME.match(name):
            if CHANGES_ANYWAY.search(name):  # findFix.../identifyFix... only detect with a literal autoFix=False
                fix = next((k.value for k in node.keywords if k.arg == "autoFix"), None)
                if not (isinstance(fix, ast.Constant) and fix.value is False):
                    self.refuse(node, "%s repairs geometry unless called with autoFix=False" % q)
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
        modules = {"np", "Q", "S"}  # names bound to Python modules: their functions are not entity methods
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.asname or a.name.split(".")[0] for a in node.names
                               if not (a.name == "apex" or a.name.startswith("apex.")))
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0 \
                    and node.module.split(".")[0] != "apex":  # from PIL import Image: Image.open is a module's
                modules.update(a.asname or a.name for a in node.names if a.name != "*")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            chain = self.chain_of(f)
            if chain is not None:
                self.apex_call(chain, node)
                continue
            if not isinstance(f, ast.Attribute):
                continue
            name = f.attr
            base, _ = _chain(f)
            if base == "B" and self.bindings["B"] == 0:  # the model-building helpers
                if name not in B_READS:
                    self.refuse(node, "B.%s() changes the model" % name)
            elif base in modules and self.bindings[base] <= 1:
                continue  # np.delete, json.dumps, Q.bbox
            elif name == "update":
                if not self.python_typed(f.value):
                    src = ast.get_source_segment(self.code, f.value) or "it"
                    hint = (" If %s is a Python dict, build it in this script with {...} or dict(), or merge "
                            "with %s |= {...}" % (src, src)) if isinstance(f.value, ast.Name) else ""
                    self.refuse(node, "%s.update(...) changes an Apex entity (Part.update and the like).%s"
                                % (src, hint))
            elif name in CONTAINER_METHODS or READ_NAME.match(name):
                continue
            elif CHANGE_VERB.match(name):
                self.refuse(node, ".%s() changes the model or the scene" % name)

    # -------------------------------------------------------------------- main
    def run(self):
        try:
            tree = ast.parse(self.code)
        except SyntaxError as exc:
            self.errors.append({"line": exc.lineno, "msg": "SyntaxError: %s" % exc.msg})
            return
        self.collect(tree)
        self.big_iteration(tree)
        if self.mode == "query":
            self.read_only(tree)
        self.errors.sort(key=lambda r: (r["line"] or 0, r["msg"]))


def check(code, mode="run"):
    """[{line, msg, query?}]: what stops a script. mode='query' adds the read-only rule of
    query_apex_python; its findings carry query=True and cannot be forced."""
    c = Checker(code, mode)
    c.run()
    return c.errors


def format_issues(items):
    return "\n".join("  line %s: %s" % (r["line"] if r["line"] is not None else "?", r["msg"]) for r in items)
