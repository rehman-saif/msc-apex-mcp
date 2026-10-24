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

            async def call(name, expect_error=False, show=300, probe=False, **a):
                res = await s.call_tool(name, a)
                txt = next((c.text for c in res.content if c.type == "text"), "")
                err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                tag = "ERR" if err else "ok"
                print("==", name, tag, txt[:show].replace("\n", " "))
                if err != expect_error or ("AssertionError" in txt) != probe:
                    FAIL.append("%s: %s" % (name, txt[:300]))
                return txt

            await call("apex_status")
            t = await call("list_tree")
            assert '"elements": 90502' in t, t
            t = await call("part_summary", paths=["Model/PID53"], sample_per_mesh=3, show=900)
            assert "PCOMP_skin" in t and "Quad4" in t
            await call("part_summary", show=200)
            t = await call("geometry_check", paths=["Model/PID54"], show=900)
            assert '"SmallEdge": 1' in t and "edge_len_min" in t
            await call("get_entity", path="Model/PID53")
            await call("find_entities", name_contains="pid")
            await call("list_connections")
            await call("list_loads_constraints")
            await call("list_catalog")
            await call("list_scenarios")
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
            assert "response too large" in t
            t = await call("run_apex_python", code="apex.mesh.noSuchFunction(x=1)", show=120)
            assert "traceback" in t and "look up the failing call" in t, t
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
