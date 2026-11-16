"""Script checks without Apex: the static check before a script runs (syntax, element loops, the
read-only rule of query_apex_python), the effects receipt and the failing-call report.
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
import apex  # noqa: E402  (the fake)
import apex_worker as W  # noqa: E402
import effects as FX  # noqa: E402
import introspect as IN  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_checks_")
W.set_inprocess({"output_dir": TMP})

# ------------------------------------------------------------------ static check
def msgs(items):
    return " | ".join("%s:%s" % (r["line"], r["msg"]) for r in items)


def refused(code):
    return [e for e in PF.check(code, "query") if e.get("query")]


def test_run_mode_stops_only_syntax_errors_and_element_loops():
    for code in ("apex.getEntites(pathNames=['a'])", "apex.mesh.createSurfaceMesh(name='m', meshsize=2.0)",
                 "x = apex.mesh.SurfaceMeshElementShape.Quad", "apex.deleteEntities(target=c)\np.update(name='x')",
                 "open('o', 'w')\nimport subprocess", "B.mesh_solid(paths=['a'], size=2.0)"):
        assert PF.check(code) == [], code  # Apex reports a wrong name or argument itself, at the call
    e = PF.check("for x in :\n    pass")
    assert len(e) == 1 and e[0]["msg"].startswith("SyntaxError") and e[0]["line"] == 1, e


def test_walking_elements_or_nodes_blocks():
    for code in ("for e in part.elements:\n    pass", "ids = [n.id for n in mesh.nodes]", "x = list(m.elements)",
                 "x = sorted(p.nodes, key=str)", "try:\n    for e in part.elements:\n        pass\nexcept Exception:\n    pass"):
        for mode in ("run", "query"):
            e = msgs(PF.check(code, mode))
            assert "iterates" in e and "getElementIDs" in e and "force=True" in e, (code, e)
    for code in ("n = len(part.elements)", "ids = mesh.getElementIDs()", "for e in elements:\n    pass",
                 "for e in part.elementsSomething:\n    pass"):
        assert PF.check(code) == [], code


def test_query_reads_pass():
    ok = ("m = apex.currentModel()\nparts = m.getParts(recursive=True)\n"
          "c = apex.EntityCollection()\nc.append(parts[0])\nc.extend(parts)\n"
          "d = {}\nd.update(a=1)\nstats = dict()\nstats.update({'n': len(parts)})\n"
          "k = apex.mesh.SurfaceMeshElementShape.Triangle\nimport numpy as np\nxyz = np.zeros(3)\n"
          "s = 'a,b'.split(',')\nt = 'x'.replace('x', 'y')\nb = Q.bbox(parts[0])\nimport os\np = os.path.join('a', 'b')\n"
          "with open('in.txt') as fh:\n    txt = fh.read()\n"
          "pc = apex.partCollection()\nimport sys\nsys.stdout.write('x')\nresult = {'n': len(parts)}\n"
          "apex.setScriptUnitSystem(unitSystemName='mm-kg-s-N-K')\nfrom PIL import Image\n"
          "im = Image.open('a.png').convert('RGB')\npt = apex.Coordinate(0.0, 0.0, 1.0)\n"
          "res = apex.geometry.findFixSmallSurfaceAndCurveFeatures(target=c, autoFix=False)\n"
          "g = Q.geometry_check(parts)\nt = B.targets('Model/P')\nx = np.delete(xyz, 0)")
    assert PF.check(ok, "query") == [], msgs(PF.check(ok, "query"))


def test_query_refuses_changes_and_force_cannot_override():
    cases = {
        "apex.deleteEntities(target=c)": "apex.deleteEntities changes",
        "f = apex.deleteEntities\nf(target=c)": "apex.deleteEntities changes",  # through an alias
        "m = apex.mesh\nm.createSurfaceMesh(name='x')": "apex.mesh.createSurfaceMesh changes",
        "from apex.mesh import createSurfaceMesh as mk\nmk(name='x')": "apex.mesh.createSurfaceMesh changes",
        "apex.session.hideAll()": "apex.session.hideAll changes",
        "apex.display.render()": "apex.display.render changes",
        "apex.geometry.findFixSmallSurfaceAndCurveFeatures(autoFix=True)": "repairs geometry unless called with autoFix=False",
        "apex.geometry.findFixSmallSurfaceAndCurveFeatures(autoFix=fix)": "repairs geometry",  # not a literal False
        "part = apex.currentModel().getParts()[0]\npart.update(name='x')": "part.update(...) changes an Apex entity",
        "apex.currentModel().getParts()[0].update(color=[1, 2, 3])": "update(...) changes",
        "def f(d):\n    d.update(a=1)": "d.update(...) changes",  # a parameter: not known to be a dict
        "p.hide()": ".hide() changes", "p.showOnly()": ".showOnly() changes", "m.save()": ".save() changes",
        "p.assignMaterial(material=mat)": ".assignMaterial() changes", "p.setName('x')": ".setName() changes",
        "B.mesh_solid(paths=['Model/P'], size=3.0)": "B.mesh_solid() changes the model",
        "try:\n    apex.deleteEntities(target=c)\nexcept Exception:\n    pass": "apex.deleteEntities changes",
    }
    for code, text in cases.items():
        got = msgs(refused(code))
        assert text in got and "use run_apex_python" in got, (code, got)
    e = msgs(refused("m = apex.mesh\nm = 3\nm.createX()"))  # m means two things: judged as a method, by its verb
    assert ".createX() changes" in e and "apex.mesh" not in e, e
    assert PF.check("import os as apex\napex.remove('x')", "query") == []  # the script's own `apex`


def test_query_does_not_police_files_or_processes():
    """Not a sandbox, and it says so: only model and scene changes are refused."""
    for code in ("open('out.txt', 'w')", "import os\nos.remove('x')", "import subprocess", "eval('1')"):
        assert PF.check(code, "query") == [], code


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
    c = err["calls"][0]
    assert (c["name"], c["kind"], c["keywords"]) == ("apex.getEntities", "function", ["pathNames", "bad"]), err
    assert c["signature"]["params"][0]["name"] == "pathNames", c  # what this Apex exposes
    err = call("run_apex_python", code="part = apex.getPart(pathName='Model/PID53')\npart.getMeshes(x=1)")["result"]["error"]
    c = err["calls"][0]
    assert (c["kind"], c["class"], c["method"]) == ("method", "Part", "getMeshes"), err
    err = call("run_apex_python", code="def f():\n    return 1/0\nf()")["result"]["error"]
    assert err["line"] == 2 and not err["calls"], err  # the script's own function is not an apex call


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
