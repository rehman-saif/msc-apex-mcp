"""Offline end-to-end test (no Apex): MCP client -> server.py -> MCP Bridge UI code (fake
apex_sdk/.NET in uifakes) -> bridge worker -> apex_worker with a fake `apex` (fakes/).
The fake raises if any tool walks part/mesh .elements or .nodes.
Run from the venv:  python tests\\test_end_to_end.py
"""
import asyncio, json, os, shutil, sys, time, importlib.util, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="apexmcp_")
PROJ = os.path.join(TMP, "proj")
shutil.copytree(ROOT, PROJ, ignore=shutil.ignore_patterns(".venv", "output", "tests", "vendor", "*_backup_*", "__pycache__"))
cfg = json.load(open(os.path.join(PROJ, "config.json")))
cfg.update(output_dir=os.path.join(PROJ, "output"), bridge_port=47199, call_timeout_s=30, max_response_kb=64)
json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
sys.path[:0] = [os.path.join(HERE, "uifakes"), os.path.join(HERE, "fakes")]
sys.path.append(os.path.join(ROOT, "apex_mcp"))
from version import __version__
ui_path = os.path.join(PROJ, "apex_palette", "MCP", "Bridge", "MCP Bridge", "src", "mcp_bridge_ui.py")
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
            want = ["apex_describe", "apex_status", "export_bdf", "list_faces", "model_overview", "part_summary",
                    "query_apex_python", "run_apex_python", "screenshot"]
            if names != want:
                FAIL.append("the server should offer exactly %s, has %s" % (want, names))

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
            assert json.loads(t)["version"] == __version__, t
            assert "warning" not in json.loads(t), t  # M9: same folder on both sides, so no warning
            assert "MSC Apex" in json.loads(t)["apex"] and json.loads(t)["model"] == "Model", t
            res = await s.call_tool("apex_status", {})  # plain text only: no JSON-in-JSON copy of the result
            assert getattr(res, "structured_content", getattr(res, "structuredContent", None)) is None, res
            tree = json.loads(await call("model_overview", show=900))
            assert tree["cols"] == ["path", "solids", "surfaces", "meshes", "elements", "nodes", "visible"], tree
            row = next(r for r in tree["parts"] if r[0] == "Model/PID53")
            assert row[4] == 90502 and row[6] is True, row  # M5: element totals from the bulk arrays
            assert "connections" in tree and "catalog" in tree and not any(k.endswith("_error") for k in tree), tree
            assert tree["scenarios"] == [] or "error" not in str(tree["scenarios"]), tree  # M10: an empty study is fine
            t = json.loads(await call("model_overview", sections=["parts"], counts=False))
            assert t["parts"][0] == "Model/PID53" and sorted(t) == ["model", "parts"], t
            await call("model_overview", sections=["nope"], expect_error=True)
            t = await call("part_summary", paths=["Model/PID53"], sample_per_mesh=3, show=900)
            assert "PCOMP_skin" in t and "Quad4" in t
            t = await call("part_summary", paths=["Model/PID54"], show=200)
            assert all("bbox" not in s for s in json.loads(t)["parts"][0]["solids"]), t
            # the CAD check is a Q helper in a read-only script, not a tool
            t = await call("query_apex_python", show=900,
                           code="result = Q.geometry_check(apex.getPart(pathName='Model/PID54'))")
            assert '"SmallEdge":1' in t and "edge_len_min" in t and "effects: none" in t, t
            # M6: face query
            t = json.loads(await call("list_faces", paths=["Model/PID54"], kind="cyl", show=200))
            rows = t["parts"][0]["rows"]
            assert t["cols"][:2] == ["id", "kind"] and rows and all(r[1] == "cyl" and abs(r[5] - 5.0) < 1e-6 for r in rows), t
            t = json.loads(await call("list_faces", paths=["Model/PID54"], normal=[0, 0, -1], near=[0, 0, 0], radius=0.1))
            assert {r[0] for r in t["parts"][0]["rows"]} == {101}, t
            # one screenshot tool: restore=False leaves the scene and the view, save_view names the camera
            t = json.loads(await call("screenshot", scene={"show_only": ["Model/PID54"], "mesh_edges": False},
                                      views=[{"preset": "iso1", "zoom": 1.2}], restore=False, max_width=600))
            assert "show_only" in t["applied"] and t["restored"] is False, t
            t = json.loads(await call("screenshot", scene={"show_all": True}, save_view="Iso A", restore=False,
                                      max_width=600))
            assert t["saved_views"] == ["Iso A"], t
            await call("screenshot", max_width=600)
            t = json.loads(await call("screenshot", scene={"show_only": ["Model/PID53"], "color_by_part": True},
                                      views=["iso1", {"saved": "Iso A"}, {"preset": "y", "zoom": 1.5}], max_width=600,
                                      show=2000))
            assert len(t["views"]) == 3 and t["sheet"] and t["restored"] and not t["warnings"], t
            await call("screenshot", scene={"show_only": ["Model/NOPE"]}, expect_error=True)
            t = await call("screenshot", scene={"highlight": [{"paths": ["Model/PID53"]}]}, expect_error=True)
            assert "highlight" in t, t  # a removed scene key is refused by name, not silently ignored
            t = await call("run_apex_python", code="result = 1")  # no gate: the first script runs
            assert t.startswith("result=1"), t
            # apex_describe: what this Apex has; element collections are never read
            t = await call("apex_describe", target="Model/PID53", show=200)
            assert "name = 'PID53'" in t and "not read" in t and "elements" in t and "AssertionError" not in t, t
            t = await call("apex_describe", target="apex.getEntities")
            assert "apex.getEntities(pathNames = None)" in t and "reference" not in t, t
            t = await call("apex_describe", target="apex.getEntitie", expect_error=True)
            assert "apex has no 'getEntitie'" in t and "getEntities" in t and "traceback" not in t, t
            await call("run_apex_python", code="part = apex.getPart(pathName='Model/PID54')")
            t = await call("apex_describe", target="$part", filter="mesh")
            assert "Model/PID54" in t and "getMeshes" in t, t
            t = await call("apex_describe", target="$part.nodes", expect_error=True)
            assert "bulk getters" in t and "traceback" not in t, t
            t = await call("apex_describe", target="Model/NoSuchPart", expect_error=True)
            assert "no entity 'Model/NoSuchPart'" in t and "traceback" not in t, t
            await call("run_apex_python", code="result = Q.mesh_summary(apex.currentModel().getParts()[0].getMeshes()[0], sample=2)")
            t = await call("run_apex_python", code="result = {'big': 'x' * 200000}")
            assert "result too large" in t
            # M2/M4: plain text, no JSON wrapper, no log path on success
            t = await call("run_apex_python", code='print("a\\nb \\"q\\"")\nresult = {"x": [1, 2]}')
            assert t == 'a\nb "q"\nresult={"x":[1,2]}\neffects: none', repr(t)
            # effects receipt: what a script created and deleted
            t = await call("run_apex_python", code="p = apex.currentModel().createPart(name='Tmp')\nresult = p.pathName")
            assert 'effects={"created":{"parts":["Model/Tmp"]}}' in t, t
            t = await call("run_apex_python", code="apex.PARTS.remove(p)", effects=True)
            assert 'effects={"deleted":{"parts":["Model/Tmp"]}}' in t, t
            t = await call("run_apex_python", code="x = 1", effects=False)
            assert "effects" not in t, t
            # a wrong keyword is not checked beforehand: Apex raises at the call, and the result shows its signature
            t = await call("run_apex_python", code="ran = 1\napex.getEntities(paths=['Model/PID53'])", show=500)
            assert "TypeError" in t and "The failing call in this Apex:\n  apex.getEntities(pathNames = None)" in t, t
            t = await call("run_apex_python", code="for x in :\n    pass", expect_error=True)
            assert "Not run" in t and "SyntaxError" in t, t
            # the helpers are in the namespace and describable
            t = await call("apex_describe", target="$B")
            assert "create_material" in t and "static_scenario" in t, t
            t = await call("apex_describe", target="$B.rbe2")
            assert "ref" in t and "attach" in t and "Rigid node tie" in t, t
            # M1: a script's own Q / S survive the next call; the helpers stay importable
            await call("run_apex_python", code="S = 5\nQ = 'mine'")
            t = await call("run_apex_python", code="import queries\nresult = [S + 1, Q, queries.rle([3, 4, 5, 9])]")
            assert t == 'result=[6,"mine","3-5,9-9"]\neffects: none', t
            await call("run_apex_python", code="import queries as Q")
            # M6: public Q helpers
            t = await call("run_apex_python", code=(
                "part = apex.getPart(pathName='Model/PID54')\n"
                "faces = Q.faces_by_id(part, [102, 101])\n"
                "mesh = apex.currentModel().getParts()[0].getMeshes()[0]\n"
                "result = [faces.len(), Q.nodes_on_face(faces, ids_only=True),\n"
                "          Q.nodes_where(mesh, lambda p: p[:, 0] < 0.025, ids_only=True),\n"
                "          Q.nodes_where(mesh, lambda p: p[:, 0] < 0.015).len(), Q.face_shape(faces[0])[0]]"))
            assert t == 'result=[2,[1,2,3,4],[1,2,3],2,"cyl"]\neffects: none', t
            # M3: trimmed traceback (script frames only)
            t = await call("run_apex_python", code="x = 1\napex.mesh.noSuchFunction(x=1)", show=400)
            assert 'File "<script>", line 2' in t and "AttributeError" in t and "apex_worker" not in t, t
            assert "script saved to" in t, t
            t = await call("run_apex_python", code="1/0")
            assert "ZeroDivisionError" in t and "The failing call" not in t, t  # not an apex call
            # sys.exit reaches the client with its output, through the real worker entry point
            t = await call("run_apex_python", code="import sys\nprint('kept')\nsys.exit(2)", show=200)
            assert "kept" in t and "SystemExit: 2" in t, t
            # a runtime failure gets the failing call's signature, and the effects made before it
            t = await call("run_apex_python", code=(
                "p = apex.currentModel().createPart(name='Half')\n"
                "apex.getEntities(pathNames=5)"), show=900)
            assert "TypeError" in t and "The failing call in this Apex:\n  apex.getEntities(pathNames = None)" in t, t
            assert 'effects before the error={"created":{"parts":["Model/Half"]}}' in t, t
            await call("run_apex_python", code="apex.PARTS.remove(p)")
            # query_apex_python: reads run; changes are refused before Apex sees them, even with force
            t = await call("query_apex_python", code="result = [p.name for p in apex.currentModel().getParts()][:1]")
            assert t.startswith('result=["') and "effects: none" in t, t
            t = await call("query_apex_python", code="p = apex.currentModel().createPart(name='Q')", expect_error=True)
            assert "createPart() changes the model" in t and "force=True does not override" in t, t
            t = await call("query_apex_python", code="B.mesh_solid(paths=['Model/PID54'], size=2.0)", expect_error=True)
            assert "B.mesh_solid() changes the model" in t, t
            t = await call("query_apex_python", code="apex.getPart(pathName='Model/PID53').update(name='x')",
                           force=True, expect_error=True)
            assert ".update(...) changes an Apex entity" in t, t
            # what the static check cannot see, the snapshot catches
            await call("run_apex_python", code="p = apex.currentModel().createPart(name='Tmp2')")
            t = await call("query_apex_python", code="getattr(apex, 'PARTS').remove(p)")
            assert t.startswith("WARNING: this query CHANGED THE MODEL") and '"deleted":{"parts":["Model/Tmp2"]}' in t, t
            cfg["script_effects"] = False  # a query is snapshotted anyway: that is its proof
            json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
            t = await call("query_apex_python", code="x = 3")
            assert t == "effects: none", t
            t = await call("run_apex_python", code="x = 3")
            assert "effects" not in t, t
            cfg.pop("script_effects")
            json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
            # config.json edits apply on the next call, without Stop/Start
            cfg["max_items"] = 1
            json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
            t = json.loads(await call("model_overview", sections=["parts"], show=120))
            assert len(t["parts"]) == 1 and t["parts_truncated"].startswith("1 of "), t
            cfg["max_items"] = 77
            json.dump(cfg, open(os.path.join(PROJ, "config.json"), "w"))
            # M8: the B helpers through run_apex_python, each run twice to prove they replace instead of duplicating
            async def build(expr, fails=None):
                t = await call("run_apex_python", code="result = B." + expr, show=260)
                if fails is not None:
                    assert "Traceback" in t and fails in t and "result=" not in t, t
                    return t
                return json.loads(next(ln for ln in t.splitlines() if ln.startswith("result="))[7:])

            for created in (True, False):
                t = await build("create_material(name='Ti', E=110000, nu=0.34, rho=4.43e-9)")
                assert t["created"] is created, t
            t = await build("assign_solid_property(paths=['Model/PID54'], material='Ti')")
            assert t["solids"] == 9 and t["property"] == "PSOLID_Ti", t
            for replaced in (0, 9):
                t = await build("mesh_solid(paths=['Model/PID54'], size=2.0)")
                assert t["parts"][0]["replaced"] == replaced and t["parts"][0]["meshes"] == 9, t
            for replaced in (False, True):
                t = await build("glue(name='G1', side1={'path': 'Model/PID54', 'faces': [101]}, "
                                "side2={'path': 'Model/PID54', 'faces': [102]})")
                assert t["replaced"] is replaced and t["side1"] == 1, t
            t = await call("query_apex_python", code="result = apex.W['deleted']")  # the old pair and its two bodies
            assert t.startswith("result=") and int(t.splitlines()[0][7:]) >= 3, t
            t = await build("beam(name='Bolt1', points=[[0, 0, 10], [0, 0, 0]], radius=4.0, material='Ti', "
                            "mesh_size=2.0, part='Model/Connections')")
            head, nut = t["nodes_at_points"]
            assert head and nut, t
            for replaced in (0, 1):
                t = await build("rbe2(ref={'path': 'Model/Connections', 'node': %d}, "
                                "attach={'path': 'Model/PID54', 'faces': [101, 102]})" % head)
                assert t["replaced"] == replaced and t["ref_node"] == head and t["regions"] == 2, t
            t = await build("rbe2(ref=[5, 5, 5], node_part='Model/Connections', attach={'path': 'Model/PID53', 'nodes': [1, 2]})")
            assert t["ref_node_created"] and t["dependent_nodes"] == 2, t
            spc = (await build("constrain(name='Fix', target={'path': 'Model/PID54', 'faces': [101]})"))["id"]
            load = "nodal_load(name='LC1', target={'path': 'Model/Connections', 'nodes': [%d]}, force=[0, 0, 100])" % nut
            ids = [(await build(load))["id"] for _ in range(2)]
            assert ids[0] == ids[1], ids  # re-created load keeps its LOAD set id
            await build("nodal_load(name='LC1', target={'path': 'Model/PID53', 'nodes': [1]}, force=[1, 0, 0], "
                        "moment=[0, 0, 1])", fails="one per load")
            t = await call("query_apex_python", code="result = str(apex.attribute.getInteractions(target=[{'path': 'Model'}])[0].getActiveRep().id)")
            rep = json.loads(t.splitlines()[0][7:])  # BCONTACT is the glue's InteractionRep id, as a string
            for replaced in (False, True):
                t = await build("static_scenario(name='SOL101', subcases=[{'name': 'LC1', 'load': 'LC1', 'constraint': 'Fix'}])")
                sub = t["subcases"][0]
                assert t["replaced"] is replaced and sub["LOAD"] == ids[0] and sub["SPC"] == spc and sub["BCONTACT"] == rep, t
                assert "warning" not in t, t
            await build("static_scenario(name='X', subcases=[{'load': 'nope'}])", fails="no load named")
            t = json.loads(await call("model_overview", sections=["loads", "constraints", "scenarios"], show=400))
            assert t["loads"]["count"] >= 1 and "SOL101" in [s["name"] for s in t["scenarios"]], t
            # M7: export writes the deck and says where; checking it is the nastran-diagnostics skill's job
            t = await call("export_bdf", expect_error=True)  # the fake's script units are refused, as in Apex
            assert "mm-t-s-N-K" in t and "unit_system" in t, t
            t = json.loads(await call("export_bdf", unit_system="mm-t-s-N-K", show=600))
            assert os.path.isfile(t["path"]) and t["bytes"] > 0 and "lint" not in t and "cards" not in t, t
            walk = "for e in apex.currentModel().getParts()[0].elements: pass"
            t = await call("run_apex_python", code=walk, expect_error=True, show=160)  # the loop guard refuses it
            assert "iterates apex.currentModel().getParts()[0].elements" in t and "force=True" in t, t
            await call("run_apex_python", code=walk, force=True,
                       show=120, probe=True)  # forced: the fake's guard catches the walk, Apex side survives
            ui.on_stop(); time.sleep(0.5)
            await call("apex_status", expect_error=True)
            ui.on_start(); time.sleep(0.3)
            await call("apex_status")  # the server reconnects by itself after Stop/Start

asyncio.run(main())
print("\n".join(ui._ST.lines[-4:]))
ui.on_stop()
print("\nRESULT:", "PASS" if not FAIL else "FAIL\n" + "\n".join(FAIL))
sys.exit(1 if FAIL else 0)
