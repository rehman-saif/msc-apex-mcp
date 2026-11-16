"""Unit tests that need neither Apex nor the bridge: BDF summary/lint, deployment check,
run_apex_python text format, API lookup, and worker handlers against the fake apex.
Run from the venv:  python tests\\test_units.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "worker"),
                os.path.join(HERE, "fakes")]

import server  # noqa: E402
import setup_helper  # noqa: E402
import apex  # noqa: E402  (the fake)
import apex_worker as W  # noqa: E402
import builders as B  # noqa: E402
import queries as Q  # noqa: E402
import serialize as S  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_units_")
W.set_inprocess({"output_dir": TMP})


def test_deployment_warning():
    assert server.deployment_warning(server.ROOT) is None
    assert server.deployment_warning(server.ROOT.upper() + os.sep) is None  # same folder, other spelling
    w = server.deployment_warning(r"C:\Users\hp\Desktop\Apex\Apex_MCP")
    assert r"from C:\Users\hp\Desktop\Apex\Apex_MCP" in w and "apex_palette" in w, w
    assert "older copy" in server.deployment_warning(None)


def test_enum_names_and_entity_type():
    """Apex enums are classes of ints and entityType is an int: names, not numbers, must come back."""
    import queries as Q

    class Visibility:
        Hidden, NotSet, Visible, thisown = 0, -1, 1, 5

    assert [Q.enum_name(v, Visibility) for v in (0, -1, 1)] == ["Hidden", "NotSet", "Visible"]
    assert Q.enum_name(7, Visibility) == "7" and Q.enum_name("Visibility.Hidden") == "Hidden"
    assert Q.enum_name(None, Visibility) == "None" and Q.enum_name(1, None) == "1"

    class Part:
        entityType, name, pathName, id = 11, "P", "Model/P", 3

    assert S.ref(Part()) == {"type": "Part", "name": "P", "path": "Model/P", "id": 3}
    Part.entityType = "EntityType.Solid"
    assert S.ref(Part())["type"] == "Solid"


def test_script_text():
    f = server.format_script_result
    assert f({"stdout": "hi\n", "result": {"a": 1}, "seconds": 0.1, "logged_to": "x.py"}) == 'hi\nresult={"a":1}'
    t = f({"stdout": "", "traceback": "Boom\n", "seconds": 0.1, "logged_to": "x.py"})
    assert t == "Boom\n[error after 0.1s; script saved to x.py]", t
    assert f({"stdout": "", "seconds": 0.1, "logged_to": "x.py"}).startswith("(ran, no output")
    assert f({"error": "response too large"}).startswith('{"error"')


def call(method, **params):
    return W.handle_request({"id": 1, "method": method, "params": params})


def load_names():
    return [x.name for x in apex.environment.getLoads()]


def raises(fn, **kw):
    try:
        fn(**kw)
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    raise AssertionError("expected an error from %s(%s)" % (fn.__name__, kw))


def test_nodal_load_keeps_the_old_load_when_the_new_values_are_bad():
    target = {"path": "Model/PID53", "nodes": [1]}
    assert B.nodal_load(name="Keep", target=target, force=[1, 0, 0])["load"] == "Keep"
    for bad in (dict(force=[1, 2]), dict(moment=[1, 2, 3, 4]), dict(force=["x", 0, 0])):
        raises(B.nodal_load, name="Keep", target=target, **bad)
        assert "Keep" in load_names(), (bad, load_names())
    assert "three" in raises(B.nodal_load, name="Keep", target=target, force=[1, 2])
    assert "one per load" in raises(B.nodal_load, name="Keep", target=target, force=[1, 0, 0], moment=[0, 0, 1])


def test_constrain_keeps_the_old_constraint_when_dof_is_bad():
    target = {"path": "Model/PID53", "nodes": [1]}
    assert B.constrain(name="KeepSpc", target=target)["constraint"] == "KeepSpc"
    assert "dof" in raises(B.constrain, name="KeepSpc", target=target, dof="79")
    assert "KeepSpc" in [x.name for x in apex.environment.getConstraints()]


def test_builders_are_helpers_in_the_script_namespace_not_bridge_methods():
    for name in ("create_material", "mesh_solid", "glue", "rbe2", "beam", "constrain", "nodal_load", "static_scenario"):
        assert name not in W.METHODS and callable(getattr(B, name)) and getattr(B, name).__doc__, name
    r = call("run_apex_python", code="result = B.create_material(name='Al', E=70000.0, nu=0.33)['material']")
    assert r["ok"] and r["result"]["result"] == "Al" and r["result"]["effects"]["created"] == {"materials": ["catalog/Al"]}, r
    r = call("run_apex_python", code="result = B.assign_solid_property(paths=['Model/PID54'], material='Al')")
    assert "PSOLID_Al" in str(r["result"]["effects"]["created"]["properties"]), r["result"]["effects"]
    r = call("run_apex_python", code="result = B.targets('Model/PID54').len()")
    assert r["result"]["result"] == 1, r


def test_a_failure_inside_a_helper_shows_the_helper_line():
    r = call("run_apex_python", code="B.mesh_solid(paths=['Model/PID54'], size=2.0, order='Cubic')")
    tb = r["result"]["traceback"]
    assert 'File "<script>", line 1' in tb and "builders.py" in tb and "W.enum(" in tb, tb
    assert "apex_worker.py" not in tb.split("ValueError")[0].replace("builders.py", ""), tb


def test_helpers_are_describable_before_any_script():
    W.STATE["ns"] = None  # as after Start: no script has run yet
    r = call("describe", target="$B")
    assert r["ok"] and "static_scenario" in str(r["result"]), r
    r = call("describe", target="$Q.geometry_check")
    assert r["ok"] and "nothing is repaired" in str(r["result"]).lower(), r


def test_model_overview_sections_and_names():
    r = call("model_overview")["result"]
    assert r["model"] == "Model" and r["cols"][0] == "path" and r["parts"][0][0] == "Model/PID53", r
    for key in ("connections", "loads", "constraints", "catalog", "scenarios"):
        assert key in r, (key, sorted(r))
    assert r["loads"]["count"] == len(r["loads"]["items"]) and "names" in r["catalog"]["materials"], r
    assert r["catalog"]["properties_2d"]["names"] == ["PCOMP_skin"], r["catalog"]  # needs the model as target
    only = call("model_overview", sections=["loads"], counts=False)["result"]
    assert sorted(only) == ["loads", "model"], only
    r = call("model_overview", sections=["parts", "nope"])
    assert not r["ok"] and "unknown section" in r["error"] and "traceback" not in r, r
    for gone in ("list_tree", "list_connections", "list_loads_constraints", "list_catalog", "list_scenarios",
                 "get_entity", "geometry_check", "viewport", "api_index"):
        assert gone not in W.METHODS, gone


def test_scenario_enums_come_back_as_names():
    class Scn:
        name, pathName, executionStatus, solutionType, subcases = "S1", "Study/S1", 3, 0, []

    class Studies:
        class ExecutionStatus:
            CompletedNoErrors, NotSubmitted = 0, 3

        class NastranSolutionType:
            Statics_101, Modes_103 = 0, 1

    class Study:
        def getScenarios(self, recursive=True): return None
        def getScenariosNastran(self): return [Scn()]

    class FakeApex:
        studies = Studies
        def getPrimaryStudy(self): return Study()

    rec = W._scenarios(FakeApex(), None, 10, None)[0]
    assert rec["executionStatus"] == "NotSubmitted" and rec["solutionType"] == "Statics_101", rec


def test_geometry_check_is_a_q_helper():
    r = Q.geometry_check(apex.getPart(pathName="Model/PID54"))
    assert r["geometry_faults"]["by_type"] == {"SmallEdge": 1, "Sliver": 1} and r["small_features"] == {"count": 1}, r
    assert "edge_len_min" in str(r) and r["parts"][0]["part"] == "Model/PID54", r


def test_script_exit_keeps_its_output_and_reports_it():
    for code, shown in (("sys.exit(0)", "SystemExit: 0"), ("sys.exit(1)", "SystemExit: 1"),
                        ("class Esc(BaseException):\n    pass\nraise Esc('boom')", "Esc: boom"),
                        ("raise KeyboardInterrupt", "KeyboardInterrupt")):
        r = call("run_apex_python", code="import sys\nprint('kept')\n" + code)
        assert r["ok"] and r["result"]["stdout"] == "kept\n", r
        assert shown in r["result"]["traceback"], r


def test_handle_request_never_raises():
    def die(_):
        raise SystemExit(3)
    W.METHODS["_die"] = die
    try:
        r = call("_die")
    finally:
        del W.METHODS["_die"]
    assert not r["ok"] and "SystemExit" in r["error"], r


def test_rows_up_to_the_limit_ceiling_are_not_cut():
    ceiling = W.limit_of({"limit": 10 ** 6})
    rows = S.to_json({"rows": [[i] for i in range(ceiling)]}, depth=50)["rows"]
    assert len(rows) == ceiling and rows[-1] == [ceiling - 1], rows[-1]
    rows = S.to_json([0] * (ceiling + 5), depth=50)
    assert rows[-1] == "... 5 more", rows[-1]


def test_part_summary_budget_covers_all_parts():
    all_parts = len(call("part_summary", sample_per_mesh=0)["result"]["parts"])
    assert all_parts > 1
    old = W.CFG.get("query_time_budget_s")
    W.CFG["query_time_budget_s"] = -1
    try:
        res = call("part_summary", sample_per_mesh=0)["result"]
    finally:
        if old is None:
            W.CFG.pop("query_time_budget_s")
        else:
            W.CFG["query_time_budget_s"] = old
    assert res["parts"] == [] and res["parts_skipped"] == all_parts and res["budget_exceeded"], res


def test_export_bdf_explains_rejected_default_units():
    r = call("export_bdf", path=os.path.join(TMP, "units.bdf"))
    assert not r["ok"], r
    for text in ("mm-kg-s-N", "script units", "unit_system", "mm-t-s-N-K", "m-kg-s-N-K"):
        assert text in r["error"], r["error"]
    r = call("export_bdf", path=os.path.join(TMP, "units.bdf"), unit_system="mm-t-s-N-K")
    assert r["ok"] and r["result"]["unit_system"] == "mm-t-s-N-K", r


def test_export_bdf_reports_a_deck_apex_did_not_write():
    real = apex.Model.exportFEModel
    apex.Model.exportFEModel = lambda self, **kw: None  # returns, writes nothing (Apex 2024.1, unmeshed model)
    try:
        r = call("export_bdf", path=os.path.join(TMP, "never.bdf"), unit_system="mm-t-s-N-K")
    finally:
        apex.Model.exportFEModel = real
    assert not r["ok"] and "wrote no deck" in r["error"] and "never.bdf" in r["error"], r
    assert "FileNotFoundError" not in r["error"], r


def test_setup_drops_retired_config_keys():
    old = setup_helper.ROOT
    setup_helper.ROOT = TMP
    try:
        with open(os.path.join(TMP, "config.example.json"), "w", encoding="utf-8") as fh:
            json.dump({"bridge_port": 47115, "bridge_token": "", "output_dir": ""}, fh)
        with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"bridge_token": "keep", "skills_dir": "x", "require_skill_lookup": True}, fh)
        setup_helper.ensure_config()
        with open(os.path.join(TMP, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    finally:
        setup_helper.ROOT = old
    assert cfg["bridge_token"] == "keep" and cfg["bridge_port"] == 47115, cfg
    assert "skills_dir" not in cfg and "require_skill_lookup" not in cfg, cfg


def test_codex_snippet_keeps_the_long_timeout():
    old = setup_helper.ROOT
    setup_helper.ROOT = TMP
    try:
        setup_helper.write_snippets()
    finally:
        setup_helper.ROOT = old
    with open(os.path.join(TMP, "codex_config.snippet.toml"), encoding="utf-8") as fh:
        assert "tool_timeout_sec = 300" in fh.read()


if __name__ == "__main__":
    tests =[(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_")]
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
