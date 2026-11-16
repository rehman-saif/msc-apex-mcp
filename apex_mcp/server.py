"""Apex MCP server (runs in the venv, Python 3.10+ with the `mcp` package).

The MCP client (Claude, Codex, ...) starts this file over stdio. Every tool is forwarded to the
"MCP Bridge" palette tool running inside the user's Apex (127.0.0.1:<bridge_port>,
authenticated with bridge_token from config.json).

It is a bridge, with few tools: status, three model reads, a screenshot, a deck export, a live
API describe and two script tools. Documentation (Apex API, scripting rules, Nastran) is not
here: it is in the nastran-fea-skills, which the client loads.

Apex executes one call at a time on its script thread. If a call is still running,
new calls are refused immediately ("Apex is busy") instead of queueing behind it -
queued calls are what turned one slow query into an Apex crash.
"""
import json
import os
import socket
import sys
import threading
import time
from typing import Optional, Union

import anyio
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import ToolAnnotations

try:  # mcp 2.x
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError

import preflight as PF
from describe_text import call_line, format_describe
from version import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("APEX_MCP_CONFIG", os.path.join(ROOT, "config.json"))

NOT_RUNNING = ("Cannot reach the MCP Bridge in Apex on 127.0.0.1:%s. In Apex open the "
               "'MCP' palette -> 'MCP Bridge' and press Start, then retry.")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("output_dir", os.path.join(ROOT, "output"))
    cfg.setdefault("call_timeout_s", 300)
    cfg.setdefault("bridge_port", 47115)
    return cfg


class Bridge:
    """TCP client to the MCP Bridge tool inside Apex. Strictly one call at a time."""

    def __init__(self):
        self.conn = self.rfile = self.wfile = None
        self.lock = threading.Lock()
        self.next_id = 0
        self.hello = None
        self.current = None  # (method, start_time) of the call in flight

    def connect(self):
        cfg = load_config()
        port = int(cfg["bridge_port"])
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            raise RuntimeError(NOT_RUNNING % port)
        self.conn = conn
        self.rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = conn.makefile("w", encoding="utf-8", newline="\n")
        try:
            self._send({"auth": cfg.get("bridge_token", "")})
            line = self.rfile.readline()
            hello = json.loads(line) if line else {}
        except (OSError, ValueError):
            hello = {}
        if hello.get("hello") != "apex-bridge":
            self.close()  # never send a request on this: it could run after the caller gave up
            error = hello.get("error")
            if error is None:
                raise RuntimeError("The MCP Bridge on 127.0.0.1:%s did not answer. Press Stop then Start "
                                   "in MCP Bridge, or restart Apex if it is not responding." % port)
            if error == "bad token":
                error += " (bridge_token in config.json must match the one Apex read when Start was pressed)"
            raise RuntimeError("MCP Bridge refused the connection: %s" % error)
        self.hello = hello

    def close(self):
        for obj in (self.rfile, self.wfile, self.conn):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.conn = self.rfile = self.wfile = None

    stop = close

    def _send(self, obj):
        self.wfile.write(json.dumps(obj) + "\n")
        self.wfile.flush()

    def call(self, method, params=None, timeout=None):
        if not self.lock.acquire(timeout=2.0):
            m, t0 = self.current or ("?", time.time())
            raise ToolError("Apex is busy with '%s' (running %.0fs). Do not resend; wait and retry "
                            "later, or ask the user whether Apex is still responsive." % (m, time.time() - t0))
        try:
            self.current = (method, time.time())
            return self._call(method, params, timeout)
        finally:
            self.current = None
            self.lock.release()

    def _closed_by_bridge(self):
        """True if the bridge dropped this connection since the last call (Stop/Start in Apex)."""
        try:
            self.conn.setblocking(False)
            return self.conn.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _call(self, method, params, timeout):
        if self.conn is not None and self._closed_by_bridge():
            self.close()
        if self.conn is None:
            self.connect()
        self.next_id += 1
        rid = self.next_id
        self.conn.settimeout(timeout or float(load_config().get("call_timeout_s", 300)) + 30)
        try:
            self._send({"id": rid, "method": method, "params": params or {}})
            line = self.rfile.readline()
        except socket.timeout:
            self.close()
            raise RuntimeError("Apex call '%s' timed out" % method)
        except OSError:
            line = ""
        if not line:  # sent, so it may have run: resending could run a model change twice
            self.close()
            raise RuntimeError("Lost connection to the MCP Bridge during '%s'. It was not resent because it "
                               "may have run: check the model before retrying." % method)
        resp = json.loads(line)
        if not resp.get("ok"):
            tb = (resp.get("traceback") or "")[-1500:]
            raise ToolError(str(resp.get("error")) + ("\n\nApex-side traceback (last lines):\n" + tb if tb else ""))
        return resp.get("result")


# Tool annotations: MCP clients can auto-allow READS and ask before CHANGES. LOOKS change only the
# view or write an output file (screenshot restores the scene it set up).
READS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LOOKS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

BRIDGE = Bridge()


def tool(annotations, **kw):
    """mcp.tool without a structured result. These tools return compact JSON (or plain text) as
    text; a structured result would repeat it as a string inside {"result": ...}, every quote
    escaped, and add an output schema to each tool definition."""
    return mcp.tool(annotations=annotations, structured_output=False, **kw)


mcp = _Server("apex", instructions=(
    "A bridge to the user's open MSC Apex session. Apex runs ONE call at a time on its script thread "
    "and shares memory with the user's model: never send Apex tools in parallel, and if a tool says "
    "Apex is busy, wait instead of resending. If the bridge is unreachable, ask the user to press "
    "Start in Apex (MCP palette -> MCP Bridge). Entities are addressed by pathName exactly as "
    "model_overview returns them. Start with apex_status and model_overview, then part_summary on "
    "the parts of interest; screenshot to look. Everything else is a script: query_apex_python "
    "reads, run_apex_python changes the model (confirm with the user first). For Apex API "
    "signatures, enum members and scripting rules use the apex-scripting skill (nastran-fea-skills); "
    "apex_describe shows what this Apex really has. Check an exported deck with the "
    "nastran-diagnostics skill."))


async def _bridge(method, params):
    try:
        return await anyio.to_thread.run_sync(lambda: BRIDGE.call(method, params))
    except ToolError:
        raise
    except Exception as exc:  # start-up / timeout problems: show the real message to the client
        raise ToolError("%s: %s" % (type(exc).__name__, exc)) from exc


def _json(obj):
    return json.dumps(obj, separators=(",", ":"), default=str)


async def _call(method, **params):
    clean = {k: v for k, v in params.items() if v is not None}
    return _json(await _bridge(method, clean))


# ------------------------------------------------------------------ session
def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def deployment_warning(bridge_root):
    """None, or why edits here do not reach Apex: its palette runs the bridge from another copy
    of this folder (or from one too old to say where it is)."""
    if bridge_root and _same_path(bridge_root, ROOT):
        return None
    where = "from %s" % bridge_root if bridge_root else "from an older copy"
    return ("Apex runs the bridge %s, this server runs from %s: edits on one side do not reach the other. "
            "Point Apex's Custom Tools (Application Settings) at %s and press Stop/Start in MCP Bridge."
            % (where, ROOT, os.path.join(ROOT, "apex_palette")))


@tool(READS)
async def apex_status() -> str:
    """Check the connection to Apex: this server's version, the Apex release, the current model,
    script and application unit systems and the project folder. A `warning` appears when Apex
    runs the bridge from another copy of this server."""
    res = await _bridge("status", {})
    app = res.get("application") or {}
    out = {"version": __version__,
           "apex": " ".join(str(app[k]) for k in ("name", "release", "buildVersion") if app.get(k)),
           "model": res.get("model")}
    for k in ("script_units", "application_units", "project_folder"):
        out[k] = res.get(k)
    warning = deployment_warning(res.get("root"))
    if warning:
        out["warning"] = warning
    return _json(out)


# ------------------------------------------------------------------- model
@tool(READS)
async def model_overview(sections: Optional[list[str]] = None, counts: bool = True,
                         limit: Optional[int] = None) -> str:
    """What the model holds. sections (default all): parts, connections, loads, constraints,
    catalog, scenarios.
    parts: every part by pathName, one row each under `cols`: [path, solids, surfaces, meshes,
    elements, nodes, visible]; the assembly tree is in the paths (an assembly path is a part path
    without its last name), and assemblies with no part below them come under empty_assemblies.
    counts=False returns the part paths only. connections: counts by type (fasteners, bolts, rigid
    links, bushings, connectors, ties, interactions such as glue/contact, point masses) and the
    first 10 of each. loads / constraints: name and id (the Nastran set id). catalog: names of
    materials, 2D/3D element properties and layered panels. scenarios: solution type, status and
    subcases. For one entity's properties use apex_describe with its pathName."""
    return await _call("model_overview", sections=sections, counts=counts, limit=limit)


@tool(READS)
async def part_summary(paths: Optional[list[str]] = None, sample_per_mesh: int = 3,
                       geometry: bool = True, limit: Optional[int] = None) -> str:
    """The safe way to understand a part (or every part under an assembly path; all parts if
    paths is omitted). Per mesh: class, element counts by topology, node count, associated
    geometry, and the property/material/thickness of `sample_per_mesh` evenly spaced elements
    (max 50). Per solid: volume, face/edge counts, min/max face area; per surface: area, free
    (exterior) edge count, manifold flag. Plus bounding boxes."""
    return await _call("part_summary", paths=paths, sample_per_mesh=sample_per_mesh,
                       geometry=geometry, limit=limit)


@tool(READS)
async def list_faces(paths: Optional[list[str]] = None, kind: Optional[str] = None,
                     near: Optional[list[float]] = None, radius: Optional[float] = None,
                     normal: Optional[list[float]] = None, angle_tol: float = 5.0,
                     limit: Optional[int] = None) -> str:
    """Find geometry faces to load, constrain, glue or tie, as compact rows under `cols`:
    [id, kind, area, centroid, dir, r]. kind is plane | cyl | curved (from sampled normals);
    dir is the plane normal or cylinder axis, r the cylinder radius. Filters: kind; near=[x,y,z]
    with radius (centroid within radius); normal=[nx,ny,nz] (dir within angle_tol degrees, either
    sense). paths are parts or assemblies (all parts if omitted). Use the ids in a script as
    targets {"path": part, "faces": [ids]} of the B helpers, or with Q.faces_by_id(part, ids)."""
    return await _call("list_faces", paths=paths, kind=kind, near=near, radius=radius, normal=normal,
                       angle_tol=angle_tol, limit=limit)


@tool(LOOKS)
async def export_bdf(path: Optional[str] = None, scenario: Optional[str] = None, hdf5: bool = True,
                     unit_system: Optional[str] = None) -> str:
    """Write the Nastran deck of a Nastran scenario (the only one, or `scenario`) to `path`
    (default output/bdf/<scenario>.bdf) and return its path, size and unit system. The deck is
    not read back: check it with the nastran-diagnostics skill (tools/deck_checks.py <path>).
    hdf5 adds MDLPRM,HDF5. unit_system defaults to config script_unit_system, else Apex's script
    units. Apex only exports consistent systems such as mm-t-s-N-K or m-kg-s-N-K and refuses
    mm-kg-s-N-K, so a model built in tonnes needs unit_system='mm-t-s-N-K'. Writes a file only."""
    return await _call("export_bdf", path=path, scenario=scenario, hdf5=hdf5, unit_system=unit_system)


# --------------------------------------------------------------- screenshot
class Scene(BaseModel):
    """What is shown and how, for the picture. Everything is optional; entities are Apex pathNames."""
    model_config = ConfigDict(extra="forbid")
    show_only: Optional[list[str]] = Field(None, description="Hide every other part, show these (parts, "
                                           "assemblies, solids, surfaces)")
    show: Optional[list[str]] = None
    hide: Optional[list[str]] = None
    show_all: bool = False
    color_by_part: bool = Field(False, description="Give each visible part its own colour (parts often share one)")
    render_style: Optional[str] = Field(None, description="Wireframe | HiddenLines | Filled | FilledWithEdges | "
                                        "Shaded | ShadedWithEdges, for every part")
    transparency: Optional[dict[str, int]] = Field(None, description="pathName -> 0-100")
    mesh_edges: Optional[bool] = None
    loads_bc_markers: Optional[bool] = None
    connection_markers: Optional[bool] = None


class View(BaseModel):
    """A camera: a preset (Apex fits it to what is visible) or a saved view, optionally zoomed."""
    model_config = ConfigDict(extra="forbid")
    preset: Optional[str] = Field(None, description="iso1-iso4, x, -x, y, -y, z, -z, right(+X), left(-X), "
                                  "back(+Y), front(-Y), top(+Z), bottom(-Z): the side the camera is on")
    saved: Optional[str] = Field(None, description="A view stored with save_view")
    zoom: Optional[float] = Field(None, description=">1 closer, <1 farther; applied after the fit")


@tool(LOOKS)
async def screenshot(scene: Optional[Scene] = None, views: Optional[list[Union[str, View]]] = None,
                     layout: str = "sheet", crop: str = "model", max_width: Optional[int] = None,
                     margin: Optional[float] = None, restore: bool = True,
                     save_view: Optional[str] = None) -> list:
    """Picture(s) of the Apex viewport. Optionally set up a scene first (show_only/hide parts,
    colour by part, render style, transparency, markers) and take up to 9 views in one call:
    presets ("iso1", "top", "front", ...), saved views, or View objects. Presets are fitted by Apex
    to what is visible, so show_only a part to frame it; there is no free rotation.
    restore=true (default) puts the user's scene and camera back afterwards; marker/edge toggles
    cannot be read back and are listed under not_restored. restore=false leaves the scene and the
    last view as set up: that is how to change what the user sees.
    save_view stores the camera as it is now under that name, usable in `views` from then on (for
    an angle no preset gives, the user sets it up in Apex first).
    layout "sheet" returns one labelled grid for several views, "separate" one image each. crop
    "model" removes Apex's floating panels by cropping to the model; "none" returns the whole
    viewport. Presets are zoomed out to `margin` (default 0.85) so the model clears the panels.
    Apex captures SCREEN PIXELS, so its window is raised if something covers it (an always-on-top
    window cannot be raised over and is reported). Without arguments it captures what is on screen."""
    res = await _bridge("screenshot", {k: v for k, v in dict(
        scene=scene.model_dump(exclude_defaults=True) if scene is not None else None,
        views=[v if isinstance(v, str) else v.model_dump(exclude_none=True) for v in views] if views else None,
        layout=layout, crop=crop, max_width=max_width, margin=margin, restore=restore,
        save_view=save_view).items() if v is not None})
    paths = [res["sheet"]] if res.get("sheet") else [v["path"] for v in res["views"]]
    return [Image(path=p) for p in paths] + [_json(res)]


# ------------------------------------------------------------------ describe
@tool(READS)
async def apex_describe(target: str = "apex", filter: Optional[str] = None, values: bool = True,
                        limit: int = 200) -> str:
    """What the RUNNING Apex has, read live; the API documentation (apex-scripting skill) can be
    wrong for this release: moved classes, enum members it leaves out, undocumented functions.
    Apex's objects carry no docstrings, so argument documentation is only in the skill.
    target:
      * an API path: 'apex.mesh' (module: its functions, classes, enums), 'apex.mesh.createSolidMesh'
        (signature, where Apex exposes one), 'apex.mesh.SurfaceMeshElementShape' (enum members with
        values), 'apex.Part' (class: properties and methods), 'apex.Part.update' (method signature);
      * an entity pathName from model_overview: its class, property values and methods;
      * '$name' or '$name.attr': a name in the script namespace: a variable left by a script
        ('$part', '$res.meshes'), or the helpers: '$Q' and '$B' list them, '$B.rbe2' gives one's
        signature and documentation.
    filter keeps member names containing that text. values=False lists an object's properties
    without reading them. Properties that can hold every element or node (elements, nodes,
    ...entities, ...IDs) are never read."""
    res = await _bridge("describe", {"target": target, "filter": filter, "values": values, "limit": limit})
    return format_describe(res)


# ------------------------------------------------------------------ scripts
def format_effects(eff, failed=False):
    """One line: what the script changed in the model (see worker/effects.py)."""
    if eff is None:
        return None
    if eff.get("error"):
        return "effects: not tracked (%s)" % eff["error"]
    body = {k: v for k, v in eff.items() if k not in ("seconds", "untracked")}
    tail = " (not tracked: %s)" % ", ".join(eff["untracked"]) if eff.get("untracked") else ""
    if not body:
        return "effects: none" + tail
    return ("effects before the error=" if failed else "effects=") + _json(body) + tail


def format_script_result(res):
    """Plain text for the script tools: stdout, then result=<compact JSON>, then what the script
    changed (effects), then the trimmed traceback. JSON-wrapping the stdout would escape every
    newline and quote in it."""
    if not isinstance(res, dict) or "stdout" not in res:  # e.g. the worker's size cap fired
        return _json(res)
    parts = []
    if res.get("stdout"):
        parts.append(res["stdout"].rstrip("\n"))
    if "result" in res:
        parts.append("result=" + _json(res["result"]))
    if res.get("result_error"):
        parts.append("result not returned: %s\npreview: %s" % (res["result_error"], res.get("result_preview", "")))
    failed = bool(res.get("traceback"))
    eff = format_effects(res.get("effects"), failed)
    if eff:
        parts.append(eff)
    if failed:
        parts.append(res["traceback"].rstrip("\n"))
        parts.append("[error after %ss; script saved to %s]" % (res.get("seconds"), res.get("logged_to")))
    return "\n".join(parts) or "(ran, no output: print() or set result = ... to return something)"


def preflight_refusal(errs):
    text = "Not run: %d problem(s) in the script:\n%s\n\nNothing was sent to Apex." % (len(errs), PF.format_issues(errs))
    if any(e.get("query") for e in errs):
        return text + (" A script that changes the model or the scene belongs in run_apex_python; force=True "
                       "does not override this.")
    return text + " Fix the script and resend it, or resend with force=True if the loop is known to be small."


def effects_changed(eff):
    """True when an effects receipt shows a change (not just timing or untracked sections)."""
    return isinstance(eff, dict) and any(k in eff for k in ("created", "deleted", "changed", "model_switched"))


async def _run_script(code, mode, force=False, effects=True):
    """run_apex_python and query_apex_python: static check, run, then the result text."""
    query = mode == "query"
    errs = PF.check(code, mode)
    if errs and (not force or errs[0]["msg"].startswith("SyntaxError") or any(e.get("query") for e in errs)):
        raise ToolError(preflight_refusal(errs))
    track = bool(effects and load_config().get("script_effects", True))  # the worker snapshots a query regardless
    res = await _bridge("run_apex_python", {"code": code, "effects": track, "mode": mode})
    out = format_script_result(res)
    eff = res.get("effects") if isinstance(res, dict) else None
    if query and effects_changed(eff):  # the static check missed something: say so first, loudly
        out = ("WARNING: this query CHANGED THE MODEL (not undone; see effects= below). The read-only check "
               "missed it: tell the user what changed. Use run_apex_python for scripts that change things.\n\n" + out)
    elif query and isinstance(eff, dict) and eff.get("error"):
        out += "\n(could not confirm that the query changed nothing: %s)" % eff["error"]
    err = res.get("error") if isinstance(res, dict) else None
    if err and err.get("calls"):
        sigs = [call_line(c) for c in err["calls"][:3]]
        out += "\n\nThe failing call%s in this Apex:\n%s" % ("s" if len(sigs) > 1 else "",
                                                           "\n".join("  " + x for x in sigs))
    return out


QUERY_DOC = """READ-ONLY Python on Apex's script thread, for anything the summary tools do not cover:
    counting, measuring, collecting ids, checking properties. Returns plain text: print() output
    (last 20k chars), then `result=<JSON>` if the script set `result = ...`. Variables persist
    between calls (and to run_apex_python). The script is refused before it runs if it would
    change the model or the scene, judged by name: an apex function that is not a read (getX,
    isX, asX, evaluateX, measureX, findX ... read; createX, deleteEntities, setX, assignX,
    display.render ... do not), a method with a changing verb (update, setX, createX, hide, show
    ...; .update() is fine on a dict the script builds itself), and the B helpers. The model is
    snapshotted before and after: if it changed anyway, the result starts with a WARNING.

    MEMORY - Apex shares this process with the user's model:
    * Never iterate part.elements / part.nodes / mesh.elements / mesh.nodes (refused; force=True
      overrides for a collection known to be small), and never read per-element attributes across
      a whole mesh.
    * Use the bulk arrays: mesh.getElementIDs() {topology: ids}, getElementConnectivities(),
      getNodeIDs(), getNodeCoordinates() (flat xyz), with numpy, which Apex ships.
    * Aggregate in the script; return counts / min / max / top-N, not raw lists.

    In the namespace: apex, and two helper modules. Q reads without walking elements (part and mesh
    summaries, faces and nodes by id or position, geometry_check); B builds a model (see
    run_apex_python). apex_describe('$Q') lists them, apex_describe('$Q.nodes_where') documents one.
    For Apex API calls, enum members and scripting rules use the apex-scripting skill."""


@tool(READS, description=QUERY_DOC)
async def query_apex_python(code: str, force: bool = False) -> str:
    return await _run_script(code, "query", force=force)


RUN_DOC = """Run Python on Apex's script thread that CHANGES the model or the scene (confirm with the
    user first); for scripts that only read use query_apex_python, whose memory rules and
    namespace apply here too. Returns plain text: print() output, `result=<JSON>` if the script
    set `result = ...`, then `effects=` (what it created, deleted or changed in the model: parts,
    assemblies, solid/surface/curve/mesh/element/node counts, loads, constraints, connections,
    materials, properties, scenarios; `effects: none` when it changed none of these), and on
    failure the traceback, the failing apex call's signature in this Apex and where the script
    was saved (output/scripts). effects=False skips the model snapshots before and after.

    B has tested, idempotent steps for a linear static model (a repeat call with the same name
    replaces what the earlier one made): B.create_material, B.assign_solid_property, B.mesh_solid,
    B.glue, B.rbe2, B.beam, B.constrain, B.nodal_load, B.static_scenario. Targets are a pathName,
    {"path": part, "faces": [ids from list_faces]} or {"path": part, "nodes": [ids]}.
    apex_describe('$B') lists them, apex_describe('$B.rbe2') gives one's arguments."""


@tool(CHANGES, description=RUN_DOC)
async def run_apex_python(code: str, force: bool = False, effects: bool = True) -> str:
    return await _run_script(code, "run", force, effects)


def main():
    try:
        mcp.run()
    finally:
        BRIDGE.stop()


if __name__ == "__main__":
    sys.exit(main())
