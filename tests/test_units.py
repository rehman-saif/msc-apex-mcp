"""Unit tests that need neither Apex nor the bridge: BDF summary/lint, deployment check,
run_apex_python text format. Run from the venv:  python tests\\test_units.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "worker")]

import bdf_summary  # noqa: E402
import server  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_units_")


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
