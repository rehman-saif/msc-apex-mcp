"""Apex MCP server (runs in the venv, Python 3.10+ with the `mcp` package).

Claude Desktop starts this file over stdio. Every tool is forwarded to the
"Claude Bridge" palette tool running inside the user's Apex (127.0.0.1:<bridge_port>,
authenticated with bridge_token from config.json).

Pre-processing only: model inspection, CAD checks, viewport and a raw-script escape
hatch. Post-processing and .h5 results tools were removed on purpose.

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
from typing import Optional

import anyio

try:  # mcp 2.x
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError

from skills import SKILLS_REPO, ApexScriptingSkill, find_skill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("APEX_MCP_CONFIG", os.path.join(ROOT, "config.json"))

NOT_RUNNING = ("Cannot reach the Claude Bridge in Apex on 127.0.0.1:%s. In Apex open the "
               "'Claude' palette -> 'Claude Bridge' and press Start, then retry.")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("output_dir", os.path.join(ROOT, "output"))
    cfg.setdefault("call_timeout_s", 300)
    cfg.setdefault("bridge_port", 47115)
    return cfg


class Bridge:
    """TCP client to the Claude Bridge tool inside Apex. Strictly one call at a time."""

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
        self._send({"auth": cfg.get("bridge_token", "")})
        line = self.rfile.readline()
        hello = json.loads(line) if line else {}
        if hello.get("hello") != "apex-bridge":
            self.close()
            raise RuntimeError("Claude Bridge refused the connection: %s (bridge_token in config.json "
                               "must match the one Apex read when Start was pressed)" % hello.get("error"))
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

    def _call(self, method, params, timeout):
        for attempt in (1, 2):
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
            if line:
                break
            self.close()  # bridge restarted or stopped: reconnect once
            if attempt == 2:
                raise RuntimeError("Lost connection to the Claude Bridge during '%s'. %s"
                                   % (method, NOT_RUNNING % load_config()["bridge_port"]))
        resp = json.loads(line)
        if not resp.get("ok"):
            raise ToolError("%s\n\nApex-side traceback (last lines):\n%s"
                            % (resp.get("error"), (resp.get("traceback") or "")[-2500:]))
        return resp.get("result")


BRIDGE = Bridge()
mcp = _Server("apex", instructions=(
    "Tools that drive the user's open MSC Apex session for pre-processing. "
    "Apex runs ONE call at a time on its script thread and shares memory with the user's model, "
    "so: never send Apex tools in parallel; prefer the summary tools; and in run_apex_python never "
    "iterate part.elements / part.nodes / mesh.elements or read per-element properties across a "
    "whole mesh (that crashed Apex on a 90k-element model). Use the bulk MeshBody arrays "
    "(getElementIDs, getNodeIDs, getNodeCoordinates, getElementConnectivities) and the Q helpers. "
    "Typical flow: apex_status -> list_tree -> part_summary / geometry_check on the parts of "
    "interest -> list_connections / list_loads_constraints / list_catalog -> viewport tools + "
    "screenshot to look. Entities are addressed by Apex pathName exactly as list_tree returns them. "
    "If a tool says the bridge is unreachable, ask the user to press Start in Apex "
    "(Claude palette -> Claude Bridge); if it says Apex is busy, wait instead of resending. "
    "WRITING APEX PYTHON: recalled Apex calls are usually wrong (keyword-only arguments, update() "
    "instead of assignment, near-miss enum names). Before the first run_apex_python of a session read "
    "apex_scripting_guide('scripting-model'), and confirm every apex call, enum member and entity "
    "property you are not certain of with apex_api_lookup. If a script fails, look the failing call "
    "up before retrying instead of guessing a new signature."))


async def _bridge(method, params):
    try:
        return await anyio.to_thread.run_sync(lambda: BRIDGE.call(method, params))
    except ToolError:
        raise
    except Exception as exc:  # start-up / timeout problems: show the real message to Claude
        raise ToolError("%s: %s" % (type(exc).__name__, exc)) from exc


async def _call(method, **params):
    clean = {k: v for k, v in params.items() if v is not None}
    res = await _bridge(method, clean)
    return json.dumps(res, indent=1, default=str)


# ------------------------------------------------------------------ session
@mcp.tool()
async def apex_status(connect: bool = True) -> str:
    """Connect to the Claude Bridge in Apex and report Apex release, current model,
    unit systems, project folder and the query limits in force."""
    return await _call("status", connect=connect)


@mcp.tool()
async def reconnect() -> str:
    """Drop and re-open the connection to the Claude Bridge (use after pressing Stop/Start in Apex)."""
    await anyio.to_thread.run_sync(BRIDGE.close)
    return await _call("status")


# ------------------------------------------------------------------- model
@mcp.tool()
async def list_tree(counts: bool = True, max_depth: int = 10, limit: Optional[int] = None) -> str:
    """Assembly/part tree with pathNames and visibility; with counts, numbers of solids, surfaces,
    meshes, elements and nodes per part (element/node totals come from bulk ID arrays)."""
    return await _call("list_tree", counts=counts, max_depth=max_depth, limit=limit)


@mcp.tool()
async def part_summary(paths: Optional[list[str]] = None, sample_per_mesh: int = 3,
                       geometry: bool = True, limit: Optional[int] = None) -> str:
    """The safe way to understand a part (or every part under an assembly path; all parts if
    paths is omitted). Per mesh: class, element counts by topology, node count, associated
    geometry, and the property/material/thickness of `sample_per_mesh` evenly spaced elements
    (max 50). Per solid: volume, face/edge counts, min/max face area; per surface: area, free
    (exterior) edge count, manifold flag. Plus bounding boxes. Read-only."""
    return await _call("part_summary", paths=paths, sample_per_mesh=sample_per_mesh,
                       geometry=geometry, limit=limit)


@mcp.tool()
async def geometry_check(paths: Optional[list[str]] = None, small_feature_tolerance: float = 0.5,
                         faults: bool = True, small_features: bool = True,
                         limit: Optional[int] = None) -> str:
    """Read-only CAD health check of the solids and surfaces in the given parts: Apex
    findGeometryFaults (Sliver, SmallEdge, SmallFace, Overhang, Gap, Crack, Spike) counted by
    type, findFixSmallSurfaceAndCurveFeatures with autoFix=False (gaps/overhangs/edges below
    small_feature_tolerance, script length units), and per-body edge-length / face-area ranges,
    free-edge counts and volumes. Nothing is repaired."""
    return await _call("geometry_check", paths=paths, small_feature_tolerance=small_feature_tolerance,
                       faults=faults, small_features=small_features, limit=limit)


@mcp.tool()
async def get_entity(path: str, properties: Optional[list[str]] = None) -> str:
    """Reference and bounding box of one entity by pathName, plus any listed Apex properties
    (e.g. ['material','thickness']; collections come back as counts). For mesh, property and
    material detail of a part use part_summary."""
    return await _call("get_entity", path=path, properties=properties)


@mcp.tool()
async def find_entities(name_contains: str, include_connections: bool = True,
                        limit: Optional[int] = None) -> str:
    """Find assemblies, parts and (optionally) connections whose name contains the text."""
    return await _call("find_entities", name_contains=name_contains,
                       include_connections=include_connections, limit=limit)


@mcp.tool()
async def list_connections(details: bool = False, properties: Optional[list[str]] = None,
                           per_type: int = 10, limit: Optional[int] = None) -> str:
    """Connections in the model (fasteners, 3D bolts, rigid links, bushings, connectors, node/
    discrete/mesh-dependent ties, interactions such as glue/contact, point masses): full counts
    by type, and names of the first `per_type` of each type. details=True adds key properties
    (attachment regions etc., as counts)."""
    return await _call("list_connections", details=details, properties=properties,
                       per_type=per_type, limit=limit)


@mcp.tool()
async def list_loads_constraints(limit: Optional[int] = None) -> str:
    """Counts and names of all loads and constraints in the environment."""
    return await _call("list_loads_constraints", limit=limit)


@mcp.tool()
async def list_catalog(limit: Optional[int] = None) -> str:
    """Names of materials (all and assigned), 2D and 3D element properties and layered panels."""
    return await _call("list_catalog", limit=limit)


@mcp.tool()
async def list_scenarios() -> str:
    """Studies and scenarios (incl. Nastran scenarios) with solution type, status and subcases."""
    return await _call("list_scenarios")


@mcp.tool()
async def import_fe_model(model_file: str, unit_system: str, organize_by: Optional[str] = None) -> str:
    """Import a Nastran BDF into the current Apex model. unit_system e.g. 'mm-kg-s-N'.
    organize_by: ByMaterial, ByProperty, ByContiguousMesh, ByElementType, SinglePart.
    CHANGES THE MODEL."""
    return await _call("import_fe_model", model_file=model_file, unit_system=unit_system,
                       organize_by=organize_by)


# ---------------------------------------------------------------- viewport
@mcp.tool()
async def visibility(action: str, paths: Optional[list[str]] = None, fit: bool = True,
                     view: str = "iso1") -> str:
    """Isolate or change what is shown. action: show_only | show | hide | show_all | hide_all.
    paths are pathNames. With fit=True the camera is reset to `view` to frame what is visible."""
    return await _call("visibility", action=action, paths=paths, fit=fit, view=view)


@mcp.tool()
async def set_view(view: Optional[str] = None, focus: Optional[list[float]] = None,
                   azimuth: Optional[float] = None, elevation: Optional[float] = None,
                   roll: Optional[float] = None, zoom: Optional[float] = None,
                   pan: Optional[list[float]] = None) -> str:
    """Move the camera. view: iso1..iso4, x, y, z (prefix '-' to look from the other side; resets
    and fits to everything displayed, including load/BC and coordinate-system markers - hide those
    with display_options to frame just the parts). Then optional rotate by azimuth/elevation
    degrees, roll, zoom factor (>1 closer) and pan=[horizontal, vertical]. focus=[x,y,z] is
    unreliable in Apex 2024.1. Returns camera state."""
    return await _call("set_view", view=view, focus=focus, azimuth=azimuth, elevation=elevation,
                       roll=roll, zoom=zoom, pan=pan)


@mcp.tool()
async def display_options(connection_markers: Optional[bool] = None, loads_bc_markers: Optional[bool] = None,
                          mesh_edges: Optional[bool] = None, paths: Optional[list[str]] = None,
                          render_style: Optional[str] = None, transparency: Optional[int] = None,
                          clear_highlights: bool = False) -> str:
    """Toggle connection markers, load/BC markers and mesh edges; set render_style
    (Wireframe, HiddenLines, Filled, FilledWithEdges, Shaded, ShadedWithEdges) or transparency
    (0-100) on parts/assemblies given by paths; optionally clear highlights."""
    return await _call("display_options", connection_markers=connection_markers,
                       loads_bc_markers=loads_bc_markers, mesh_edges=mesh_edges, paths=paths,
                       render_style=render_style, transparency=transparency,
                       clear_highlights=clear_highlights)


@mcp.tool()
async def highlight(part_path: Optional[str] = None, ids: Optional[list[int]] = None, kind: str = "Element",
                    paths: Optional[list[str]] = None, color: Optional[list[int]] = None) -> str:
    """Highlight elements/nodes by ID within a part (part_path + ids, kind Element|Node; capped by
    max_highlight_ids) or whole entities by pathName, in an RGB color (default magenta)."""
    return await _call("highlight", part_path=part_path, ids=ids, kind=kind, paths=paths, color=color)


@mcp.tool()
async def annotate(text: Optional[str] = None, location: Optional[list[float]] = None, size: int = 14,
                   color: Optional[list[int]] = None, clear: bool = False) -> str:
    """Draw a text label in the viewport at location [x,y,z], or clear=True to remove all labels."""
    return await _call("annotate", text=text, location=location, size=size, color=color, clear=clear)


@mcp.tool()
async def cut_view(location: Optional[list[float]] = None, slice: bool = False, off: bool = False) -> str:
    """Section the view with a cut plane through location (default: model centre). off=True removes it."""
    return await _call("cut_view", location=location, slice=slice, off=off)


@mcp.tool()
async def screenshot(max_width: int = 1400) -> list:
    """Capture the Apex viewport as a PNG and return it as an image."""
    res = await _bridge("screenshot", {"max_width": max_width})
    return [Image(path=res["path"]), json.dumps(res)]


# ------------------------------------------------------------------- skill
# The apex-scripting skill (nastran-fea-skills, cloned into vendor/ by setup.bat) is served
# from here so every MCP client gets it, not only clients that load skills themselves.
_SKILL = {"obj": None, "consulted": False}
NO_SKILL = ("The apex-scripting skill is not installed, so Apex calls cannot be checked. Run setup.bat "
            "again (it clones %s into vendor/), or set skills_dir in config.json." % SKILLS_REPO)


def _skill():
    if _SKILL["obj"] is None:
        path = find_skill(load_config())
        if path:
            _SKILL["obj"] = ApexScriptingSkill(path)
    return _SKILL["obj"]


@mcp.tool()
async def apex_scripting_guide(topic: str = "scripting-model") -> str:
    """Rules for writing Apex Python, from the apex-scripting skill. Read 'scripting-model' before the
    first run_apex_python of a session. Other topics: pitfalls, patterns-and-recipes,
    module-selection, debugging-playbook, script-quality-checklist, remote-and-batch,
    custom-tool-authoring, gui-sdk-reference, gui-sdk-patterns, documentation-index,
    api-readme, overview. Local and read-only; does not touch Apex."""
    sk = _skill()
    if sk is None:
        raise ToolError(NO_SKILL)
    _SKILL["consulted"] = True
    return sk.guide(topic)


@mcp.tool()
async def apex_api_lookup(query: str, kind: str = "any", max_results: int = 5) -> str:
    """Exact Apex API signatures (Iberian Lynx FP2) from the apex-scripting skill's generated
    reference: 989 functions, 733 classes, 237 enums. query is a name ('createSurfaceMesh',
    'apex.mesh.CurveMesh', 'Quad4') or a few words ('assign property 2d'). kind: any | function |
    class | enum. Returns full signatures with per-argument docs, class properties/methods and
    enum members. Use it before writing any apex call you are not certain of. Local and read-only."""
    sk = _skill()
    if sk is None:
        raise ToolError(NO_SKILL)
    _SKILL["consulted"] = True
    return sk.lookup(query, kind=kind, max_results=max_results)


# --------------------------------------------------------------------- raw
@mcp.tool()
async def run_apex_python(code: str, attach: bool = True) -> str:
    """Run Python on Apex's script thread with the open Apex's `apex` module (keyword-only API).
    Variables persist between calls. Set `result = ...` to return a value; print() output is
    returned too (last 20k chars). Every script is saved to output/scripts.

    MEMORY RULES - Apex shares this process with the user's model:
    * Never iterate part.elements / part.nodes / mesh.elements / mesh.nodes, and never read
      per-element attributes (material, elementProperty2D, ...) across a whole mesh.
    * Use bulk arrays: mesh.getElementIDs() {topology: ids}, mesh.getElementConnectivities(),
      mesh.getNodeIDs(), mesh.getNodeCoordinates() (flat xyz) - with numpy, which Apex ships.
    * Aggregate in the script; return counts / min / max / top-N, not raw lists.
    * Helpers preloaded as Q: Q.part_summary(part), Q.mesh_summary(mesh, sample=3),
      Q.sample_elements(mesh, n), Q.topology_counts(mesh), Q.solid_summary(solid),
      Q.surface_summary(surf), Q.bbox(entity), Q.Budget(seconds) (`if b.over(): break`).
    * Model changes: confirm with the user first.

    API RULES - generated Apex code usually fails on these:
    * Every argument by keyword; Apex accepts no positional arguments.
    * Change entity properties with entity.update(...); plain assignment silently does nothing.
    * Pass targets as apex.EntityCollection (appendList), use .len() not len().
    * Confirm calls, enum members and properties with apex_api_lookup first. The first call of a
      session is refused until apex_scripting_guide or apex_api_lookup has been used."""
    cfg = load_config()
    sk = _skill()
    if sk is not None and cfg.get("require_skill_lookup", True) and not _SKILL["consulted"]:
        raise ToolError("Not run. Read apex_scripting_guide('scripting-model') first, and check the "
                        "apex calls in this script with apex_api_lookup, then resend it. (Enforced once per "
                        "session; set require_skill_lookup to false in config.json to turn it off.)")
    out = await _call("run_apex_python", code=code, attach=attach)
    if sk is None:
        return "NOTE: %s\n\n%s" % (NO_SKILL, out)
    try:
        failed = bool(json.loads(out).get("traceback"))
    except (ValueError, AttributeError):
        failed = False
    if failed:  # script errors come back as a result with a traceback, not as a ToolError
        out += ("\n\nThe script raised. Before retrying, look up the failing call with apex_api_lookup "
                "(and apex_scripting_guide('pitfalls')) instead of guessing a new signature.")
    return out


def main():
    try:
        mcp.run()
    finally:
        BRIDGE.stop()


if __name__ == "__main__":
    sys.exit(main())
