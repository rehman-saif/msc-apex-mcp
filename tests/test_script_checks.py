"""run_apex_python checks without Apex: preflight (static), the effects receipt, the failing-call
report and the API-reference signature lookups behind them.
Run from the venv:  python tests\\test_script_checks.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "worker"),
                os.path.join(HERE, "fakes")]

import preflight as PF  # noqa: E402
import skills  # noqa: E402
import apex  # noqa: E402  (the fake)
import apex_worker as W  # noqa: E402
import effects as FX  # noqa: E402
import introspect as IN  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_checks_")
W.set_inprocess({"output_dir": TMP})

# ------------------------------------------------------------------ preflight fixtures
DOC = {"modules": {"apex", "apex.mesh", "apex.session"},
       "functions": {"apex.getEntities": [["pathNames"]],
                     "apex.mesh.createSurfaceMesh": [["name", "target", "meshSize", "meshType"]],
                     "apex.mesh.getCurveMeshes": [["list"]],
                     "apex.session.hideAll": [[]]},
       "classes": {"apex.EntityCollection"},
       "enums": {"apex.mesh.SurfaceMeshElementShape": ["Mixed", "Quadrilateral", "Triangle"],
                 "apex.ConstraintAxis": ["X", "Y", "Z"],
                 "apex.mesh.Gone": ["A"]}}


def mod(functions=None, enums=None, classes=(), modules=(), other=()):
    return {"functions": functions or {}, "enums": enums or {}, "classes": list(classes),
            "modules": list(modules), "other": list(other)}


LIVE = {"modules": {
    "apex": mod({"getEntities": [["pathNames", True]], "currentModel": [], "beginUndoIndent": []},
                classes=["EntityCollection"], modules=["mesh", "session", "os"]),
    "apex.mesh": mod({"createSurfaceMesh": None, "getCurveMeshes": None},
                     enums={"SurfaceMeshElementShape": ["Mixed", "Quadrilateral", "Triangle", "Undefined"]}),
    "apex.session": mod({"hideAll": []}, enums={"ConstraintAxis": ["X", "Y", "Z"]}),
}}
OV = {"moved": {"apex.ConstraintAxis": "apex.session.ConstraintAxis"}, "absent": ["apex.mesh.Gone"],
      "absent_methods": {"createStaticStep": "studies.Scenario.createStaticStep"},
      "notes": {"apex.session.hideAll": "cannot be restored"}}


def pf(code, live=True):
    return PF.check(code, DOC, LIVE if live else None, OV)


def msgs(items):
    return " | ".join("%s:%s" % (r["line"], r["msg"]) for r in items)


def test_unknown_name_blocks_with_the_live_index_and_only_warns_from_the_docs():
    r = pf("apex.getEntites(pathNames=['a'])")
    assert "1:apex.getEntites does not exist in this Apex" in msgs(r["errors"]) and "apex.getEntities" in msgs(r["errors"]), r
    r = pf("apex.getEntites(pathNames=['a'])", live=False)
    assert not r["errors"] and "not in the API reference" in msgs(r["warnings"]), r
    # a name Apex has but the docs lack is fine when Apex is asked
    assert pf("x = apex.beginUndoIndent()") == {"errors": [], "warnings": []}


def test_aliases_are_followed_and_dropped_when_rebound():
    r = pf("m = apex.mesh\nS = m.SurfaceMeshElementShape\nm.nope()\nk = S.Quad")
    assert "3:apex.mesh.nope does not exist" in msgs(r["errors"]) and "4:apex.mesh.SurfaceMeshElementShape has no member 'Quad'" in msgs(r["errors"]), r
    r = pf("from apex.mesh import SurfaceMeshElementShape as SMS, nope\nx = SMS.Undefined")
    assert msgs(r["errors"]).count("apex.mesh.nope does not exist") == 1 and "Undefined" not in msgs(r["errors"]), r
    assert pf("m = apex.mesh\nm = 3\nm.nope()")["errors"] == []  # m means two things: not checked
    assert pf("m = apex.mesh\nfor m in things:\n    m.nope()")["errors"] == []
    assert pf("import os as apex\napex.nope()")["errors"] == []  # the script's own `apex`
    r = pf("import apex.mesh\napex.mesh.nope()")
    assert "apex.mesh.nope" in msgs(r["errors"]), r


def test_enum_members_block_only_with_the_live_index():
    assert pf("x = apex.mesh.SurfaceMeshElementShape.Undefined")["errors"] == []  # runtime-only member
    r = pf("x = apex.mesh.SurfaceMeshElementShape.Quad", live=False)
    assert not r["errors"] and "per the API reference" in msgs(r["warnings"]), r


def test_arguments_are_checked_against_runtime_and_documented_signatures():
    e = msgs(pf("apex.getEntities(paths=['a'])")["errors"])
    assert "has no argument 'paths'" in e and "Did you mean pathNames" in e and "missing required argument(s): pathNames" in e, e
    e = msgs(pf("apex.mesh.createSurfaceMesh(name='m', meshsize=2.0)")["errors"])  # (*args, **kwargs) at runtime
    assert "has no argument 'meshsize'" in e and "Did you mean meshSize" in e, e
    assert "takes 0 argument(s)" in msgs(pf("apex.beginUndoIndent(1)")["errors"])
    assert pf("apex.getEntities(['a'])")["errors"] == []  # positional is accepted by Apex
    assert pf("opts = {}\napex.mesh.createSurfaceMesh(**opts)")["errors"] == []
    assert pf("args = []\napex.getEntities(*args)")["errors"] == []


def test_moved_and_absent_names_block_even_without_the_live_index():
    for live in (True, False):
        e = msgs(pf("a = apex.ConstraintAxis.X\nb = apex.mesh.Gone.A", live=live)["errors"])
        assert "apex.ConstraintAxis is apex.session.ConstraintAxis" in e and "apex.mesh.Gone is in the API reference but not" in e, (live, e)


def test_walking_elements_or_nodes_blocks():
    for code in ("for e in part.elements:\n    pass", "ids = [n.id for n in mesh.nodes]", "x = list(m.elements)",
                 "x = sorted(p.nodes, key=str)"):
        e = msgs(pf(code)["errors"])
        assert "iterates" in e and "getElementIDs" in e and "force=True" in e, (code, e)
    for code in ("n = len(part.elements)", "ids = mesh.getElementIDs()", "for e in elements:\n    pass",
                 "for e in part.elementsSomething:\n    pass"):
        assert pf(code)["errors"] == [], code


def test_guarded_uses_only_warn():
    r = pf("try:\n    apex.nope()\nexcept Exception:\n    pass")
    assert not r["errors"] and "not blocking" in msgs(r["warnings"]), r
    r = pf("if hasattr(apex, 'nope'):\n    apex.nope()")
    assert not r["errors"], r
    r = pf("try:\n    apex.nope()\nexcept KeyError:\n    pass")  # a narrow except does not expect this
    assert r["errors"], r


def test_notes_absent_methods_syntax_and_dunders():
    r = pf("apex.session.hideAll()\nscn.createStaticStep(name='s')")
    assert "1:apex.session.hideAll: cannot be restored" in msgs(r["warnings"]), r
    assert "2:studies.Scenario.createStaticStep is in the API reference but not" in msgs(r["warnings"]), r
    assert pf("def createStaticStep(name):\n    pass\nx.createStaticStep(name='s')")["warnings"] == []
    assert pf("x = apex.__name__\ny = apex.mesh.SurfaceMeshElementShape.__mro__") == {"errors": [], "warnings": []}
    e = pf("for x in :\n    pass")["errors"]
    assert e and e[0]["msg"].startswith("SyntaxError") and e[0]["line"] == 1, e


def test_errors_carry_the_function_name_for_signatures():
    e = pf("apex.getEntities(paths=['a'])")["errors"]
    assert {x.get("name") for x in e} == {"apex.getEntities"}, e


# ------------------------------------------------------------------ query_apex_python (read-only)
QLIVE = dict(LIVE, method_names=["update", "getMeshes", "frobnicateMesh", "assignMaterial", "replace"])
QLIVE["modules"] = dict(LIVE["modules"], apex=dict(LIVE["modules"]["apex"], functions=dict(
    LIVE["modules"]["apex"]["functions"], deleteEntities=[["target", True]], findFixSmallSurfaceAndCurveFeatures=None,
    partCollection=None, setScriptUnitSystem=None)))


def q(code):
    return PF.check(code, DOC, QLIVE, OV, mode="query")


def refused(code):
    r = q(code)
    return [e for e in r["errors"] if e.get("query")]


def test_query_reads_pass():
    ok = ("m = apex.currentModel()\nparts = m.getParts(recursive=True)\n"
          "c = apex.EntityCollection()\nc.append(parts[0])\nc.extend(parts)\n"
          "d = {}\nd.update(a=1)\nstats = dict()\nstats.update({'n': len(parts)})\n"
          "k = apex.mesh.SurfaceMeshElementShape.Triangle\nimport numpy as np\nxyz = np.zeros(3)\n"
          "s = 'a,b'.split(',')\nt = 'x'.replace('x', 'y')\nb = Q.bbox(parts[0])\nimport os\np = os.path.join('a', 'b')\n"
          "with open('in.txt') as fh:\n    txt = fh.read()\nwith open('in.bin', 'rb') as fh:\n    raw = fh.read()\n"
          "pc = apex.partCollection()\nimport sys\nsys.stdout.write('x')\nresult = {'n': len(parts)}\n"
          "apex.setScriptUnitSystem(unitSystemName='mm-kg-s-N-K')\nfrom PIL import Image\n"
          "im = Image.open('a.png').convert('RGB')\n"
          "res = apex.findFixSmallSurfaceAndCurveFeatures(target=c, autoFix=False)")
    r = q(ok)
    assert r["errors"] == [], msgs(r["errors"])


def test_query_refuses_apex_changes_and_cannot_be_softened():
    cases = {
        "apex.deleteEntities(target=c)": "apex.deleteEntities changes",
        "f = apex.deleteEntities": "apex.deleteEntities changes",  # referenced, not called
        "apex.mesh.createSurfaceMesh(name='x')": "apex.mesh.createSurfaceMesh changes",
        "apex.session.hideAll()": "apex.session.hideAll changes",
        "from apex.mesh import createSurfaceMesh": "apex.mesh.createSurfaceMesh changes",
        "apex.findFixSmallSurfaceAndCurveFeatures(autoFix=True)": "repairs geometry unless called with autoFix=False",
        "apex.findFixSmallSurfaceAndCurveFeatures(autoFix=fix)": "repairs geometry",  # not a literal False
        "part = apex.currentModel().getParts()[0]\npart.update(name='x')": "part.update(...) changes an Apex entity",
        "apex.currentModel().getParts()[0].update(color=[1, 2, 3])": "update(...) changes",
        "def f(d):\n    d.update(a=1)": "d.update(...) changes",  # a parameter: not known to be a dict
        "p.hide()": ".hide() changes", "p.showOnly()": ".showOnly() changes", "m.save()": ".save() writes a file",
        "p.assignMaterial(material=mat)": ".assignMaterial() changes", "p.setName('x')": ".setName() changes",
        "s.frobnicateMesh()": ".frobnicateMesh() changes",  # an Apex method (live index), unknown verb
        "try:\n    apex.deleteEntities(target=c)\nexcept Exception:\n    pass": "apex.deleteEntities changes",
    }
    for code, text in cases.items():
        got = msgs(refused(code))
        assert text in got and "use run_apex_python" in got, (code, got)


def test_query_refuses_files_processes_and_dynamic_code():
    cases = {
        "open('out.txt', 'w')": "writes a file", "open('o.txt', mode='a')": "writes a file",
        "open('o.txt', m)": "mode set at run time", "import io\nio.open('o', 'wb')": "writes a file",
        "import os\nos.remove('x')": "os.remove", "import os as o\no.system('dir')": "os.system",
        "from os import remove": "os.remove", "import subprocess": "import subprocess",
        "from shutil import rmtree": "from shutil", "import numpy as np\nnp.save('a.npy', x)": "numpy.save() writes",
        "from pathlib import Path\nPath('a').write_text('x')": ".write_text() writes",
        "exec('x = 1')": "exec()", "eval('1')": "eval()", "setattr(p, 'name', 'x')": "setattr()",
    }
    for code, text in cases.items():
        got = msgs(refused(code))
        assert text in got, (code, got)
    r = q("getattr(p, name)()")
    assert not r["errors"] and "chosen at run time" in msgs(r["warnings"]), r


def test_run_mode_does_not_apply_the_read_only_checks():
    r = pf("apex.deleteEntities(target=c)\np.update(name='x')\nopen('o', 'w')\nimport subprocess")
    assert not [e for e in r["errors"] if e.get("query")], r


# ------------------------------------------------------------------ effects receipt
def snap(**kw):
    base = {"model": "Model", "partial": [], "errors": {}, "assemblies": [], "parts": {}, "loads": [],
            "constraints": [], "materials": [], "properties": [], "connections": [], "scenarios": [], "seconds": 0.01}
    base.update(kw)
    return base


def test_diff_names_created_deleted_and_changed():
    b = snap(parts={"M/A": {"meshes": 0, "elements": 0}, "M/B": {"meshes": 1}}, loads=["F", "F"])
    a = snap(parts={"M/A": {"meshes": 2, "elements": 900}, "M/C": {"meshes": 0}}, loads=["F"], materials=["Steel"])
    d = FX.diff(b, a)
    assert d == {"created": {"parts": ["M/C"], "materials": ["Steel"]}, "deleted": {"parts": ["M/B"], "loads": ["F"]},
                 "changed": {"M/A": {"meshes": [0, 2], "elements": [0, 900]}}}, d
    assert FX.diff(b, b) == {}


def test_diff_skips_sections_that_did_not_finish_and_caps_lists():
    b = snap(parts={"M/A": {}}, partial=["parts"])
    a = snap(parts={"M/A": {}, "M/B": {}}, loads=["L%d" % i for i in range(30)])
    d = FX.diff(b, a)
    assert "parts" not in d.get("created", {}) and d["untracked"] == ["parts"], d
    assert len(d["created"]["loads"]) == FX.MAX_NAMES + 1 and d["created"]["loads"][-1] == "... +10 more", d
    assert FX.diff(snap(), snap(model="Other")) == {"model_switched": ["Model", "Other"]}
    d = FX.diff(snap(loads=["A"], partial=["loads"]), snap(loads=["A", "B"], errors={"materials": "boom"}, materials=["M"]))
    assert d == {"untracked": ["loads", "materials"]}, d


def test_snapshot_reads_the_model_without_walking_elements():
    s = FX.snapshot(W, budget_s=5.0)
    assert s["parts"]["Model/PID53"]["elements"] == 90502 and s["parts"]["Model/PID54"]["solids"] == 9, s["parts"]
    assert "Clamp" in s["constraints"] and not s["partial"], s
    s = FX.snapshot(W, budget_s=-1.0)  # out of time at once (0 can tie on a coarse clock): all not tracked
    assert "parts" in s["partial"] and "loads" in s["partial"], s


def call(method, **params):
    return W.handle_request({"id": 1, "method": method, "params": params})


def test_run_apex_python_reports_effects_also_when_it_fails():
    r = call("run_apex_python", code="p = apex.currentModel().createPart(name='E1')\nraise ValueError('late')")
    res = r["result"]
    assert res["effects"]["created"] == {"parts": ["Model/E1"]} and "ValueError" in res["traceback"], res
    apex.PARTS.remove(next(p for p in apex.PARTS if p.name == "E1"))
    assert "effects" not in call("run_apex_python", code="x = 1", effects=False)["result"]
    r = call("run_apex_python", code="x = 1", effects=False, mode="query")["result"]  # a query: always
    assert r["effects"] == {"seconds": r["effects"]["seconds"]} and "query_" in r["logged_to"], r


# ------------------------------------------------------------------ failing call
def test_the_failing_call_is_resolved_in_its_frame():
    code = ("part = apex.getPart(pathName='Model/PID53')\n"
            "x = apex.getEntities(pathNames=[apex.currentModel().pathName], bad=1)\n")
    err = call("run_apex_python", code=code)["result"]["error"]
    assert err["line"] == 2 and err["type"] == "TypeError", err
    assert err["calls"][0] == {"name": "apex.getEntities", "kind": "function", "keywords": ["pathNames", "bad"]}, err
    err = call("run_apex_python", code="part = apex.getPart(pathName='Model/PID53')\npart.getMeshes(x=1)")["result"]["error"]
    c = err["calls"][0]
    assert (c["kind"], c["class"], c["method"]) == ("method", "Part", "getMeshes"), err
    err = call("run_apex_python", code="def f():\n    return 1/0\nf()")["result"]["error"]
    assert err["line"] == 2 and not err["calls"], err  # the script's own function is not an apex call


# ------------------------------------------------------------------ live index
def test_api_index_is_static_and_finds_enums():
    class Shape:
        Mixed, Quadrilateral = 0, 1
        thisown = property(lambda self: True)
    assert IN.enum_members(Shape) == ["Mixed", "Quadrilateral"]
    assert IN.enum_members(apex.Part) is None  # reading Part.elements would raise in the fake
    r = call("api_index")
    assert r["ok"] and os.path.isfile(r["result"]["path"]), r


# ------------------------------------------------------------------ signatures
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_signatures_come_from_the_reference_including_inherited_methods():
    d = os.path.join(TMP, "skill", "api")
    write(os.path.join(d, "modules", "apex.md"),
          "## apex\n\n### `apex.getEntities(pathNames: [str]) -> apex.EntityCollection`\nGet.\n\n"
          "### `apex.f(a: {str:[int]}, b: int = 2) -> None`\nF.\n\n### `apex.f(c: str) -> None`\nF2.\n")
    write(os.path.join(d, "classes", "apex.md"),
          "## `apex.Assembly`  (extends `Entity`)\nAsm.\n\nMethods:\n\n"
          "- `createPart(name: str = \"\") -> Part` — Create a part.\n- `update(name: str) -> None` — Update.\n\n"
          "## `apex.Part`  (extends `Entity`)\nPart.\n\nMethods:\n\n- `getMeshes() -> MeshCollection` — Meshes.\n")
    write(os.path.join(d, "methods-index.md"),
          "# Method lookup index\n\n- `apex.Assembly.createPart` — Create a part. (inherited by `Model`)\n"
          "- `apex.Assembly.update` — Update. (inherited by `Model`)\n- `apex.Part.getMeshes` — Meshes.\n")
    for name in ("enums.md", "functions-index.md", "classes.md"):
        write(os.path.join(d, name), "")
    sk = skills.ApexScriptingSkill(os.path.dirname(d))
    assert sk.signature("apex.getEntities") == ["apex.getEntities(pathNames: [str]) -> apex.EntityCollection"]
    assert len(sk.signature("apex.f")) == 2 and sk.doc_index()["functions"]["apex.f"] == [["a", "b"], ["c"]]
    s = sk.method_signature("Model", "createPart")
    assert s == ['Model.createPart(name: str = "") -> Part — Create a part. (inherited from apex.Assembly)'], s
    assert sk.method_signature("Part", "getMeshes") == ["Part.getMeshes() -> MeshCollection — Meshes."]
    assert sk.method_signature("Part", "update") == []  # Assembly.update is not Part's
    assert skills.split_params("a: {str:[int]}, b: (int, int) = (1, 2), c") == ["a", "b", "c"]


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for n, fn in tests:
        try:
            fn()
            print("ok  ", n)
        except Exception as exc:  # report every failure, not only the first
            failed += 1
            print("FAIL", n, "-", type(exc).__name__, exc)
    print("\nRESULT:", "PASS" if not failed else "FAIL (%d)" % failed)
    sys.exit(1 if failed else 0)
