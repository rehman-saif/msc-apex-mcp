"""Offline end-to-end test (no Apex): MCP client -> server.py -> Claude Bridge UI code (fake
apex_sdk/.NET in uifakes) -> bridge worker -> apex_worker with a fake `apex` (fakes/).
The fake raises if any tool walks part/mesh .elements or .nodes.
Run from the venv:  python tests\\test_end_to_end.py
"""
import asyncio, json, os, shutil, sys, time, importlib.util, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="apexmcp_")
PROJ = os.path.join(TMP, "proj")
shutil.copytree(ROOT, PROJ, ignore=shutil.ignore_patterns(".venv", "output", "tests", "vendor", "*_backup_*", "__pycache__"))
SKILLS = next((d for d in (os.environ.get("APEX_SKILLS_DIR"), os.path.join(ROOT, "vendor", "nastran-fea-skills"),
                           os.path.join(os.path.dirname(ROOT), "nastran-fea-skills")) if d and os.path.isdir(d)), None)
if not SKILLS:
    sys.exit("nastran-fea-skills not found: run setup.bat or set APEX_SKILLS_DIR")
cfg = json.load(open(os.path.join(PROJ, "config.json")))
cfg.update(output_dir=os.path.join(PROJ, "output"), bridge_port=47199, call_timeout_s=30, max_response_kb=64,
           skills_dir=SKILLS, require_skill_lookup=True)
json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
sys.path[:0] = [os.path.join(HERE, "uifakes"), os.path.join(HERE, "fakes")]
ui_path = os.path.join(PROJ, "apex_palette", "Claude", "Bridge", "Claude Bridge", "src", "claude_bridge_ui.py")
spec = importlib.util.spec_from_file_location("ui", ui_path); ui = importlib.util.module_from_spec(spec); spec.loader.exec_module(ui)
tp = ui.getUIContent(); print("panel:", tp.TitleText)
ui.on_start(); time.sleep(0.3); print("status:", ui._status_text())
ui.on_selftest(); time.sleep(2); print("\n".join(ui._ST.lines))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
FAIL = []


async def main():
    env = dict(os.environ, APEX_MCP_CONFIG=os.path.join(PROJ, "config.json"))
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(PROJ, "apex_mcp", "server.py")], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            print("TOOLS", len(names), names)
            for gone in ("contour", "deform", "probe", "clear_plots", "attach_results", "results_top_n"):
                if gone in names:
                    FAIL.append("post tool still present: " + gone)

            async def call(tool, expect_error=False, show=300, probe=False, **a):
                res = await s.call_tool(tool, a)
                txt = next((c.text for c in res.content if c.type == "text"), "")
                err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                tag = "ERR" if err else "ok"
                print("==", tool, tag, txt[:show].replace("\n", " "))
                if err != expect_error or ("AssertionError" in txt) != probe:
                    FAIL.append("%s: %s" % (tool, txt[:300]))
                return txt

            t = await call("apex_status")
            dep = json.loads(t)["deployment"]  # M9: same folder on both sides, so no warning
            assert dep["bridge_root"] and "warning" not in dep, dep
            t = await call("list_tree")
            assert '"elements":90502' in t, t  # M5: compact JSON
            t = await call("part_summary", paths=["Model/PID53"], sample_per_mesh=3, show=900)
            assert "PCOMP_skin" in t and "Quad4" in t
            t = await call("part_summary", paths=["Model/PID54"], show=200)
            assert all("bbox" not in s for s in json.loads(t)["parts"][0]["solids"]), t
            t = await call("geometry_check", paths=["Model/PID54"], show=900)
            assert '"SmallEdge":1' in t and "edge_len_min" in t
            await call("get_entity", path="Model/PID53")
            await call("find_entities", name_contains="pid")
            await call("list_connections")
            await call("list_loads_constraints")
            await call("list_catalog")
            t = await call("list_scenarios")
            assert "error" not in t, t  # M10: empty Nastran scenario list is not an error
            # M6: face query
            t = json.loads(await call("list_faces", paths=["Model/PID54"], kind="cyl", show=200))
            rows = t["parts"][0]["rows"]
            assert t["cols"][:2] == ["id", "kind"] and rows and all(r[1] == "cyl" and abs(r[5] - 5.0) < 1e-6 for r in rows), t
            t = json.loads(await call("list_faces", paths=["Model/PID54"], normal=[0, 0, -1], near=[0, 0, 0], radius=0.1))
            assert {r[0] for r in t["parts"][0]["rows"]} == {101}, t
            await call("visibility", action="show_only", paths=["Model/PID54"])
            await call("set_view", view="iso1", zoom=1.2)
            await call("highlight", part_path="Model/PID53", ids=[1, 2, 3])
            await call("screenshot", max_width=600)
            t = await call("run_apex_python", code="result = 1", expect_error=True)  # gate: no lookup yet
            assert "apex_scripting_guide" in t, t
            t = await call("apex_scripting_guide", topic="pitfalls")
            assert "Positional arguments" in t
            t = await call("apex_scripting_guide", topic="nope")
            assert "Unknown topic" in t and "scripting-model" in t
            t = await call("apex_api_lookup", query="createSurfaceMesh", show=200)
            assert "apex.mesh.createSurfaceMesh(name: str" in t
            t = await call("apex_api_lookup", query="Quad4", kind="enum")
            assert "apex.mesh.ElementTopology" in t
            t = await call("apex_api_lookup", query="apex.mesh.CurveMesh", kind="class")
            assert "update(name: str, meshSize: float" in t
            await call("run_apex_python", code="result = Q.mesh_summary(apex.currentModel().getParts()[0].getMeshes()[0], sample=2)")
            t = await call("run_apex_python", code="result = {'big': 'x' * 200000}")
            assert "result too large" in t
            # M2/M4: plain text, no JSON wrapper, no log path on success
            t = await call("run_apex_python", code='print("a\\nb \\"q\\"")\nresult = {"x": [1, 2]}')
            assert t == 'a\nb "q"\nresult={"x":[1,2]}', repr(t)
            # M1: a script's own Q / S survive the next call; the helpers stay importable
            await call("run_apex_python", code="S = 5\nQ = 'mine'")
            t = await call("run_apex_python", code="import queries\nresult = [S + 1, Q, queries.rle([3, 4, 5, 9])]")
            assert t == 'result=[6,"mine","3-5,9-9"]', t
            await call("run_apex_python", code="import queries as Q")
            # M6: public Q helpers
            t = await call("run_apex_python", code=(
                "part = apex.getPart(pathName='Model/PID54')\n"
                "faces = Q.faces_by_id(part, [102, 101])\n"
                "mesh = apex.currentModel().getParts()[0].getMeshes()[0]\n"
                "result = [faces.len(), Q.nodes_on_face(faces, ids_only=True),\n"
                "          Q.nodes_where(mesh, lambda p: p[:, 0] < 0.025, ids_only=True),\n"
                "          Q.nodes_where(mesh, lambda p: p[:, 0] < 0.015).len(), Q.face_shape(faces[0])[0]]"))
            assert t == 'result=[2,[1,2,3,4],[1,2,3],2,"cyl"]', t
            # M3: trimmed traceback (script frames only) and the reminder only once
            t = await call("run_apex_python", code="x = 1\napex.mesh.noSuchFunction(x=1)", show=400)
            assert 'File "<script>", line 2' in t and "AttributeError" in t and "apex_worker" not in t, t
            assert "script saved to" in t and "look up the failing call" in t, t
            t = await call("run_apex_python", code="1/0")
            assert "ZeroDivisionError" in t and "look up the failing call" not in t, t
            # M8: builders, each run twice to prove they replace instead of duplicating
            for created in (True, False):
                t = json.loads(await call("create_material", name="Ti", E=110000, nu=0.34, rho=4.43e-9))
                assert t["created"] is created, t
            t = json.loads(await call("assign_solid_property", paths=["Model/PID54"], material="Ti"))
            assert t["solids"] == 9 and t["property"] == "PSOLID_Ti", t
            for replaced in (0, 9):
                t = json.loads(await call("mesh_solid", paths=["Model/PID54"], size=2.0))
                assert t["parts"][0]["replaced"] == replaced and t["parts"][0]["meshes"] == 9, t
            for replaced in (False, True):
                t = json.loads(await call("glue", name="G1", side1={"path": "Model/PID54", "faces": [101]},
                                          side2={"path": "Model/PID54", "faces": [102]}))
                assert t["replaced"] is replaced and t["side1"] == 1, t
            t = json.loads(await call("beam", name="Bolt1", points=[[0, 0, 10], [0, 0, 0]], radius=4.0,
                                      material="Ti", mesh_size=2.0, part="Model/Connections"))
            head, nut = t["nodes_at_points"]
            assert head and nut, t
            for replaced in (0, 1):
                t = json.loads(await call("rbe2", ref={"path": "Model/Connections", "node": head},
                                          attach={"path": "Model/PID54", "faces": [101, 102]}))
                assert t["replaced"] == replaced and t["ref_node"] == head and t["regions"] == 2, t
            t = json.loads(await call("rbe2", ref=[5, 5, 5], node_part="Model/Connections",
                                      attach={"path": "Model/PID53", "nodes": [1, 2]}))
            assert t["ref_node_created"] and t["dependent_nodes"] == 2, t
            t = json.loads(await call("constrain", name="Fix", target={"path": "Model/PID54", "faces": [101]}))
            spc = t["id"]
            ids = [json.loads(await call("nodal_load", name="LC1", target={"path": "Model/Connections", "nodes": [nut]},
                                         force=[0, 0, 100]))["id"] for _ in range(2)]
            assert ids[0] == ids[1], ids  # re-created load keeps its LOAD set id
            await call("nodal_load", name="LC1", target={"path": "Model/PID53", "nodes": [1]}, expect_error=True,
                       force=[1, 0, 0], moment=[0, 0, 1])
            for replaced in (False, True):
                t = json.loads(await call("static_scenario", name="SOL101",
                                          subcases=[{"name": "LC1", "load": "LC1", "constraint": "Fix"}]))
                sub = t["subcases"][0]
                assert t["replaced"] is replaced and sub["LOAD"] == ids[0] and sub["SPC"] == spc and sub["BCONTACT"] == 1, t
                assert "warning" not in t, t
            await call("static_scenario", name="X", subcases=[{"load": "nope"}], expect_error=True)
            # M7: export with summary + lint (the fake deck has two deliberate faults)
            t = json.loads(await call("export_bdf", show=600))
            assert t["sol"] == "101" and "SUBCASE 1" in t["case_control"] and t["cards"]["GRID"] == 2, t
            lint = " | ".join(t["lint"])
            assert "no load card has set id" in lint and "also SPC'd" in lint, lint
            await call("run_apex_python", code="for e in apex.currentModel().getParts()[0].elements: pass",
                       show=120, probe=True)  # forbidden walk is caught and reported, Apex side survives
            ui.on_stop(); time.sleep(0.5)
            await call("apex_status", expect_error=True)
            ui.on_start(); time.sleep(0.3)
            await call("reconnect")

asyncio.run(main())
print("\n".join(ui._ST.lines[-4:]))
ui.on_stop()
print("\nRESULT:", "PASS" if not FAIL else "FAIL\n" + "\n".join(FAIL))
sys.exit(1 if FAIL else 0)
