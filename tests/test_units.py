"""Unit tests that need neither Apex nor the bridge: BDF summary/lint, deployment check,
run_apex_python text format, API lookup, and worker handlers against the fake apex.
Run from the venv:  python tests\\test_units.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "worker"),
                os.path.join(HERE, "fakes")]

import bdf_summary  # noqa: E402
import server  # noqa: E402
import setup_helper  # noqa: E402
import skills  # noqa: E402
import apex  # noqa: E402  (the fake)
import apex_worker as W  # noqa: E402
import serialize as S  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_units_")
W.set_inprocess({"output_dir": TMP})


def deck(text, name="t.bdf"):
    path = os.path.join(TMP, name)
    with open(path, "w") as fh:
        fh.write(text.lstrip("\n"))
    return path


def lint_of(text):
    return " | ".join(bdf_summary.summarize(deck(text))["lint"])


GOOD = """
SOL 101
CEND
SUBCASE 1
  BCONTACT = 1
  SPC = 1
  LOAD = 2
BEGIN BULK
$ comment
GRID    1               0.      0.      0.
GRID,2,,1.,0.,0.
GRID*   3                               2.              0.
*       0.
GRID    4               3.      0.      0.
CTETRA  10      7       1       2       3       4
PSOLID  7       5
MAT1    5       70000.          .3
BCONECT 1       1       1       2       1
SPC1    1       123456  1       THRU    2
FORCE   2       3               1.      0.      0.      1.
RBE2    20      4       123456  3       1.-5
ENDDATA
"""


def test_good_deck_is_clean():
    r = bdf_summary.summarize(deck(GOOD))
    assert r["sol"] == "101", r
    assert r["case_control"] == ["SUBCASE 1", "BCONTACT = 1", "SPC = 1", "LOAD = 2"], r["case_control"]
    assert r["cards"]["GRID"] == 4 and r["cards"]["CTETRA"] == 1, r["cards"]
    assert r["lint"] == ["no problems found by these checks"], r["lint"]


def test_contact_without_bcontact():
    t = lint_of(GOOD.replace("  BCONTACT = 1\n", ""))
    assert "no BCONTACT in the case control" in t and "BCONECT x1" in t, t


def test_bcontact_id_and_missing_sets():
    t = lint_of(GOOD.replace("BCONTACT = 1", "BCONTACT = 4").replace("LOAD = 2", "LOAD = 9"))
    assert "BCONTACT = 4 but no BCONECT" in t and "LOAD = 9 but no load card" in t, t
    t = lint_of(GOOD.replace("  SPC = 1\n", ""))
    assert "subcase 1 has no SPC" in t, t


def test_global_case_control_applies_to_subcases():
    t = lint_of(GOOD.replace("CEND\nSUBCASE 1\n  BCONTACT = 1\n  SPC = 1\n",
                             "CEND\nBCONTACT = 1\nSPC = 1\nSUBCASE 1\n"))
    assert t == "no problems found by these checks", t


def test_rbe2_dependent_spcd_and_twice():
    t = lint_of(GOOD.replace("RBE2    20      4       123456  3       1.-5",
                             "RBE2    20      4       123456  1       3\n"
                             "RBE2    21      4       123456  3"))
    assert "1 RBE2 dependent node(s) are also SPC'd" in t, t  # node 1, via SPC1 THRU
    assert "1 node(s) are dependent in more than one RBE2 (nodes [3]" in t, t


def test_rbe2_lint_compares_the_actual_dofs():
    # Decks of these shapes went through MSC Nastran 2024.1: the disjoint ones ran without a fatal
    # message, the one with a shared component stopped with UFM 2101.
    rbe2, spc = "RBE2    20      4       123456  3       1.-5", "SPC1    1       123456  1       THRU    2"
    legal = GOOD.replace(rbe2, "RBE2    20      4       123     3").replace(spc, spc + "\nSPC1    1       456     3")
    assert lint_of(legal) == "no problems found by these checks", lint_of(legal)
    fatal = GOOD.replace(rbe2, "RBE2    20      4       123     3").replace(spc, spc + "\nSPC1    1       23      3")
    assert "1 RBE2 dependent node(s) are also SPC'd" in lint_of(fatal), lint_of(fatal)
    plain = GOOD.replace(rbe2, "RBE2    20      4       123     3").replace(spc, spc + "\nSPC     1       3       12      0.")
    assert "also SPC'd" in lint_of(plain), lint_of(plain)
    disjoint = GOOD.replace(rbe2, "RBE2    20      4       123     3\nRBE2    21      1       456     3")
    assert lint_of(disjoint) == "no problems found by these checks", lint_of(disjoint)
    shared = GOOD.replace(rbe2, "RBE2    20      4       123     3\nRBE2    21      1       345     3")
    assert "1 node(s) are dependent in more than one RBE2 (nodes [3]" in lint_of(shared), lint_of(shared)


def test_scalar_points_are_not_undefined_grids():
    spc = "SPC1    1       123456  1       THRU    2"
    t = lint_of(GOOD.replace(spc, spc + "\nSPOINT  500     THRU    502\nSPC1    1       0       501"))
    assert t == "no problems found by these checks", t
    t = lint_of(GOOD.replace(spc, spc + "\nSPC1    1       0       501"))
    assert "SPC1 references 1 undefined GRID id(s): [501]" in t, t


def test_material_is_only_checked_where_the_card_has_one():
    t = lint_of(GOOD.replace("PSOLID  7       5", "PSOLID  7       5\nPCOMPLS 9       1\nPGAP    8       1"))
    assert t == "no problems found by these checks", t


def test_cross_references():
    t = lint_of(GOOD.replace("PSOLID  7       5", "PSOLID  7       6").replace("CTETRA  10      7", "CTETRA  10      8"))
    assert "reference property 8, which is not defined" in t and "PSOLID 7 references material 6" in t, t
    t = lint_of(GOOD.replace("PSOLID  7       5", "PSHELL  7       5       1."))
    assert "reference property 7, which is a shell property" in t, t
    t = lint_of(GOOD.replace("CTETRA  10      7       1       2       3       4",
                             "CTETRA  10      7       1       2       3       99"))
    assert "CTETRA references 1 undefined GRID id(s): [99]" in t, t
    t = lint_of(GOOD.replace("GRID    4 ", "GRID    1 "))
    assert "duplicate GRID" in t, t


def test_include_and_bulk_only():
    deck("GRID    9               0.      0.      0.\n", "inc.bdf")
    r = bdf_summary.summarize(deck(GOOD.replace("ENDDATA", "INCLUDE 'inc.bdf'\nENDDATA"), "main.bdf"))
    assert r["cards"]["GRID"] == 5, r["cards"]
    r = bdf_summary.summarize(deck("BEGIN BULK\nGRID    1               0.      0.      0.\nBCONECT 1\n"))
    assert r["case_control"] == [] and r["lint"] == ["no problems found by these checks"], r


def test_deployment_check():
    mine = server.file_hashes(server.ROOT)
    same = {"deployment": {"root": server.ROOT, "config_path": server.CONFIG_PATH, "hashes": dict(mine)}}
    d = server.deployment_check(same)
    assert "warning" not in d and d["bridge_root"] == server.ROOT, d
    other = dict(mine, **{"apex_mcp/worker/queries.py": "000000000000"})
    d = server.deployment_check({"deployment": {"root": r"C:\Users\hp\Desktop\Apex\Apex_MCP",
                                                "config_path": r"C:\Users\hp\Desktop\Apex\Apex_MCP\config.json",
                                                "hashes": other}})
    assert "another folder" in d["warning"] and "1 file(s) differ" in d["warning"], d
    assert list(d["differing_files"]) == ["apex_mcp/worker/queries.py"], d
    assert "older copy" in server.deployment_check({})["warning"]


def test_script_text():
    f = server.format_script_result
    assert f({"stdout": "hi\n", "result": {"a": 1}, "seconds": 0.1, "logged_to": "x.py"}) == 'hi\nresult={"a":1}'
    t = f({"stdout": "", "traceback": "Boom\n", "seconds": 0.1, "logged_to": "x.py"})
    assert t == "Boom\n[error after 0.1s; script saved to x.py]", t
    assert "x.py" in f({"stdout": "", "seconds": 0.1, "logged_to": "x.py"}, show_log=True)
    assert f({"error": "response too large"}).startswith('{"error"')


ENUMS = """# Enumeration reference

- `apex.AutoManual`: `Automatic`, `Manual`
- `apex.mesh.Old`: `A`, `B` — DEPRECATED
- `apex.EntityType` — 3 members, wrapped below — DEPRECATED
  - `apex.EntityType`: `Extension`, `%s`
  - `apex.EntityType`: `Far`
- `apex.studies.NastranSolutionType` — 3 members, wrapped below
  - `apex.studies.NastranSolutionType`: `Statics_101`, `Modes_103`
  - `apex.studies.NastranSolutionType`: `Nonlinear_400`
""" % "`, `".join("Filler%d" % i for i in range(300))


def skill():
    path = os.path.join(TMP, "skill")
    os.makedirs(os.path.join(path, "api"), exist_ok=True)
    for name, text in (("enums.md", ENUMS), ("functions-index.md", ""), ("classes.md", "")):
        with open(os.path.join(path, "api", name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return skills.ApexScriptingSkill(path)


def test_lookup_finds_enums_wrapped_over_several_lines():
    sk = skill()
    assert len(sk.enums()) == 4, sk.enums()
    t = sk.lookup("NastranSolutionType", kind="enum")
    assert "`Statics_101`" in t and "`Nonlinear_400`" in t, t
    assert "NastranSolutionType" in sk.lookup("Statics_101", kind="enum")
    assert "`A`, `B` — DEPRECATED" in sk.lookup("apex.mesh.Old", kind="enum")


def test_lookup_keeps_deprecated_note_and_the_member_asked_for_when_cut():
    t = skill().lookup("Far", kind="enum")
    assert "`apex.EntityType`: DEPRECATED" in t and "truncated" in t and "`Far`" in t, t


def call(method, **params):
    return W.handle_request({"id": 1, "method": method, "params": params})


def load_names():
    return [x.name for x in apex.environment.getLoads()]


def test_nodal_load_keeps_the_old_load_when_the_new_values_are_bad():
    target = {"path": "Model/PID53", "nodes": [1]}
    assert call("nodal_load", name="Keep", target=target, force=[1, 0, 0])["ok"]
    for bad in (dict(force=[1, 2]), dict(moment=[1, 2, 3, 4]), dict(force=["x", 0, 0])):
        r = call("nodal_load", name="Keep", target=target, **bad)
        assert not r["ok"] and "Keep" in load_names(), (bad, r["error"], load_names())
    assert "three" in call("nodal_load", name="Keep", target=target, force=[1, 2])["error"]


def test_constrain_keeps_the_old_constraint_when_dof_is_bad():
    target = {"path": "Model/PID53", "nodes": [1]}
    assert call("constrain", name="KeepSpc", target=target)["ok"]
    r = call("constrain", name="KeepSpc", target=target, dof="79")
    assert not r["ok"] and "dof" in r["error"], r
    assert "KeepSpc" in [x.name for x in apex.environment.getConstraints()]


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
