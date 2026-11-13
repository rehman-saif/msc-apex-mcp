"""Apex MCP server (runs in the venv, Python 3.10+ with the `mcp` package).

The MCP client (Claude, Codex, ...) starts this file over stdio. Every tool is forwarded to the
"MCP Bridge" palette tool running inside the user's Apex (127.0.0.1:<bridge_port>,
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
from typing import Any, Optional, Union

import anyio
from pydantic import BaseModel, Field
from mcp.types import ToolAnnotations

try:  # mcp 2.x
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError

import bdf_summary
import preflight as PF
from describe_text import format_describe
from skills import SKILLS_REPO, ApexScriptingSkill, find_skill
from version import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("APEX_MCP_CONFIG", os.path.join(ROOT, "config.json"))
sys.path.insert(0, os.path.join(ROOT, "apex_mcp", "worker"))
from apex_worker import file_hashes  # noqa: E402  (pure Python; apex is imported lazily)

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
mcp = _Server("apex", instructions=(
    "Tools that drive the user's open MSC Apex session for pre-processing. "
    "Apex runs ONE call at a time on its script thread and shares memory with the user's model, "
    "so: never send Apex tools in parallel; prefer the summary tools; and in a script never "
    "iterate part.elements / part.nodes / mesh.elements or read per-element properties across a "
    "whole mesh (that crashed Apex on a 90k-element model). Use the bulk MeshBody arrays "
    "(getElementIDs, getNodeIDs, getNodeCoordinates, getElementConnectivities) and the Q helpers. "
    "Typical flow: apex_status -> list_tree -> part_summary / geometry_check on the parts of "
    "interest -> list_connections / list_loads_constraints / list_catalog -> viewport / "
    "screenshot to look (one screenshot call can isolate parts, set colours and take several views). Entities are addressed by Apex pathName exactly as list_tree returns them. "
    "To build a model prefer the builder tools (create_material, assign_solid_property, mesh_solid, "
    "glue, rbe2, beam, constrain, nodal_load, static_scenario; faces by id from list_faces) over "
    "run_apex_python; they change the model, so confirm with the user first. For a read the summary "
    "tools do not cover, use query_apex_python: it refuses scripts that would change anything, so "
    "it needs no confirmation; keep run_apex_python for changes. Check a deck with "
    "export_bdf, which returns only case control, card counts and lint warnings. "
    "If a tool says the bridge is unreachable, ask the user to press Start in Apex "
    "(MCP palette -> MCP Bridge); if it says Apex is busy, wait instead of resending. "
    "WRITING APEX PYTHON: recalled Apex calls are usually wrong (argument names, update() "
    "instead of assignment, near-miss enum names). Both script tools check a script before running it "
    "and report what it changed in the model (effects=...). Before the first script of a session read "
    "apex_scripting_guide('scripting-model'), and confirm every apex call, enum member and entity "
    "property you are not certain of with apex_api_lookup (the documentation) or apex_describe (what this "
    "Apex really has: modules, signatures, enum members, an entity's properties and values). If a script "
    "fails, look the failing call up before retrying instead of guessing a new signature."))


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


def deployment_check(res):
    """Compare the folder (and file hashes) this server runs from with the one the Apex bridge
    loaded its worker and config from. They drift apart when Apex's palette points at another
    copy of Apex_MCP: then edits here never reach Apex."""
    theirs = res.pop("deployment", None) or {}
    out = {"server_root": ROOT, "server_config": CONFIG_PATH}
    if not theirs:
        out["warning"] = ("the bridge's apex_worker.py does not report its folder, so it is an older copy "
                          "than this server's. Register %s in Apex (Application Settings -> Custom Tools) "
                          "and press Stop/Start in MCP Bridge." % os.path.join(ROOT, "apex_palette"))
        return out
    out["bridge_root"], out["bridge_config"] = theirs.get("root"), theirs.get("config_path")
    mine, other = file_hashes(ROOT), theirs.get("hashes") or {}
    diff = {rel: {"server": h, "bridge": other.get(rel)} for rel, h in mine.items() if h != other.get(rel)}
    warn = []
    if not _same_path(out["bridge_root"] or "", ROOT):
        warn.append("Apex runs the bridge from another folder than this server")
    elif not _same_path(out["bridge_config"] or "", CONFIG_PATH):
        warn.append("the bridge reads a different config.json than this server")
    if diff:
        out["differing_files"] = diff
        warn.append("%d file(s) differ between the two copies" % len(diff))
    if warn:
        out["warning"] = ("; ".join(warn) + ". Edits on one side do not reach the other. Point Apex's "
                          "Custom Tools at %s and press Stop/Start in MCP Bridge, or copy the files."
                          % os.path.join(ROOT, "apex_palette"))
    return out


async def _status(connect=True):
    res = await _bridge("status", {"connect": connect})
    res["deployment"] = deployment_check(res)
    res["version"] = __version__
    return _json(res)


@mcp.tool(annotations=READS)
async def apex_status(connect: bool = True) -> str:
    """Connect to the MCP Bridge in Apex and report this server's version, Apex release, current model,
    unit systems, project folder and the query limits in force. `deployment` compares the
    folder this server runs from with the one the Apex bridge loads its worker and config.json
    from (plus file hashes) and warns when they differ."""
    return await _status(connect)


@mcp.tool(annotations=LOOKS)
async def reconnect() -> str:
    """Drop and re-open the connection to the MCP Bridge (use after pressing Stop/Start in Apex)."""
    await anyio.to_thread.run_sync(BRIDGE.close)
    return await _status()


# ------------------------------------------------------------------- model
@mcp.tool(annotations=READS)
async def list_tree(counts: bool = True, max_depth: int = 10, limit: Optional[int] = None) -> str:
    """Assembly/part tree with pathNames and visibility; with counts, numbers of solids, surfaces,
    meshes, elements and nodes per part (element/node totals come from bulk ID arrays)."""
    return await _call("list_tree", counts=counts, max_depth=max_depth, limit=limit)


@mcp.tool(annotations=READS)
async def part_summary(paths: Optional[list[str]] = None, sample_per_mesh: int = 3,
                       geometry: bool = True, limit: Optional[int] = None) -> str:
    """The safe way to understand a part (or every part under an assembly path; all parts if
    paths is omitted). Per mesh: class, element counts by topology, node count, associated
    geometry, and the property/material/thickness of `sample_per_mesh` evenly spaced elements
    (max 50). Per solid: volume, face/edge counts, min/max face area; per surface: area, free
    (exterior) edge count, manifold flag. Plus bounding boxes. Read-only."""
    return await _call("part_summary", paths=paths, sample_per_mesh=sample_per_mesh,
                       geometry=geometry, limit=limit)


@mcp.tool(annotations=READS)
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


@mcp.tool(annotations=READS)
async def get_entity(path: str, properties: Optional[list[str]] = None) -> str:
    """Reference and bounding box of one entity by pathName, plus any listed Apex properties
    (e.g. ['material','thickness']; collections come back as counts). For mesh, property and
    material detail of a part use part_summary."""
    return await _call("get_entity", path=path, properties=properties)


@mcp.tool(annotations=READS)
async def list_faces(paths: Optional[list[str]] = None, kind: Optional[str] = None,
                     near: Optional[list[float]] = None, radius: Optional[float] = None,
                     normal: Optional[list[float]] = None, angle_tol: float = 5.0,
                     limit: Optional[int] = None) -> str:
    """Find geometry faces to load, constrain, glue or tie, as compact rows under `cols`:
    [id, kind, area, centroid, dir, r]. kind is plane | cyl | curved (from sampled normals);
    dir is the plane normal or cylinder axis, r the cylinder radius. Filters: kind; near=[x,y,z]
    with radius (centroid within radius); normal=[nx,ny,nz] (dir within angle_tol degrees, either
    sense). paths are parts or assemblies (all parts if omitted). Use the ids in builder targets
    {"path": part, "faces": [ids]} or with Q.faces_by_id in run_apex_python. Read-only."""
    return await _call("list_faces", paths=paths, kind=kind, near=near, radius=radius, normal=normal,
                       angle_tol=angle_tol, limit=limit)


@mcp.tool(annotations=READS)
async def find_entities(name_contains: str, include_connections: bool = True,
                        limit: Optional[int] = None) -> str:
    """Find assemblies, parts and (optionally) connections whose name contains the text."""
    return await _call("find_entities", name_contains=name_contains,
                       include_connections=include_connections, limit=limit)


@mcp.tool(annotations=READS)
async def list_connections(details: bool = False, properties: Optional[list[str]] = None,
                           per_type: int = 10, limit: Optional[int] = None) -> str:
    """Connections in the model (fasteners, 3D bolts, rigid links, bushings, connectors, node/
    discrete/mesh-dependent ties, interactions such as glue/contact, point masses): full counts
    by type, and names of the first `per_type` of each type. details=True adds key properties
    (attachment regions etc., as counts)."""
    return await _call("list_connections", details=details, properties=properties,
                       per_type=per_type, limit=limit)


@mcp.tool(annotations=READS)
async def list_loads_constraints(limit: Optional[int] = None) -> str:
    """Counts and names of all loads and constraints in the environment."""
    return await _call("list_loads_constraints", limit=limit)


@mcp.tool(annotations=READS)
async def list_catalog(limit: Optional[int] = None) -> str:
    """Names of materials (all and assigned), 2D and 3D element properties and layered panels."""
    return await _call("list_catalog", limit=limit)


@mcp.tool(annotations=READS)
async def list_scenarios() -> str:
    """Studies and scenarios (incl. Nastran scenarios) with solution type, status and subcases."""
    return await _call("list_scenarios")


@mcp.tool(annotations=CHANGES)
async def import_fe_model(model_file: str, unit_system: str, organize_by: Optional[str] = None) -> str:
    """Import a Nastran BDF into the current Apex model. unit_system e.g. 'mm-kg-s-N'.
    organize_by: ByMaterial, ByProperty, ByContiguousMesh, ByElementType, SinglePart.
    CHANGES THE MODEL."""
    return await _call("import_fe_model", model_file=model_file, unit_system=unit_system,
                       organize_by=organize_by)


@mcp.tool(annotations=LOOKS)
async def export_bdf(path: Optional[str] = None, scenario: Optional[str] = None, summary: bool = True,
                     hdf5: bool = True, unit_system: Optional[str] = None) -> str:
    """Write the Nastran deck of a Nastran scenario (the only one, or `scenario`) to `path`
    (default output/bdf/<scenario>.bdf). With summary=True the deck is read here, not in Apex,
    and only this comes back: SOL, case control lines, card counts and lint warnings (contact
    cards without BCONTACT or with a BCONTACT id that matches nothing; subcases without LOAD/SPC
    or pointing at missing sets; RBE2 dependent nodes that are SPC'd or in two RBE2s; missing
    property/material/GRID references; duplicate ids). hdf5 adds MDLPRM,HDF5. unit_system
    defaults to config script_unit_system, else Apex's script units. Apex only exports consistent
    systems such as mm-t-s-N-K or m-kg-s-N-K, and refuses mm-kg-s-N-K, so a model built in tonnes
    needs unit_system='mm-t-s-N-K'. Writes a file only."""
    res = await _bridge("export_bdf", {k: v for k, v in dict(
        path=path, scenario=scenario, hdf5=hdf5, unit_system=unit_system).items() if v is not None})
    if summary:
        try:
            res.update(await anyio.to_thread.run_sync(lambda: bdf_summary.summarize(res["path"])))
        except Exception as exc:
            res["summary_error"] = "%s: %s" % (type(exc).__name__, exc)
    return _json(res)


# ---------------------------------------------------------------- viewport
# One scene description drives both tools: `viewport` applies it and leaves it, `screenshot`
# applies it, takes the pictures and puts the user's scene back (worker/scene.py).
class Style(BaseModel):
    """How things are drawn. Fields left out are not touched."""
    render_style: Optional[str] = Field(None, description="Wireframe | HiddenLines | Filled | FilledWithEdges | "
                                        "Shaded | ShadedWithEdges. Applied to `paths`, or to every part if paths is omitted.")
    paths: Optional[list[str]] = Field(None, description="Entities render_style applies to")
    color: Optional[dict[str, list[int]]] = Field(None, description="pathName -> [r, g, b] (0-255)")
    color_by_part: bool = Field(False, description="Give each visible part its own colour. Parts often share one "
                                "colour, which makes isolated or multi-part pictures hard to read.")
    transparency: Optional[dict[str, int]] = Field(None, description="pathName -> 0-100")
    mesh_edges: Optional[bool] = None
    loads_bc_markers: Optional[bool] = None
    connection_markers: Optional[bool] = None


class Highlight(BaseModel):
    paths: Optional[list[str]] = Field(None, description="Whole entities by pathName")
    part_path: Optional[str] = Field(None, description="With ids: elements or nodes inside this part")
    ids: Optional[list[int]] = None
    kind: str = Field("Element", description="Element | Node (with ids)")
    color: Optional[list[int]] = Field(None, description="[r, g, b], default magenta")


class Label(BaseModel):
    text: str
    at: list[float] = Field(description="[x, y, z] in the script unit system")
    size: int = 14
    color: Optional[list[int]] = None


class Cut(BaseModel):
    location: Optional[list[float]] = Field(None, description="[x, y, z], default model centre")
    slice: bool = False
    off: bool = Field(False, description="Hide the cut view again")


class View(BaseModel):
    """A camera: a preset (Apex fits it to what is visible) or a saved view, optionally zoomed."""
    name: Optional[str] = Field(None, description="Label used in the result and on a contact sheet")
    preset: Optional[str] = Field(None, description="iso1-iso4, x, -x, y, -y, z, -z, right(+X), left(-X), "
                                  "back(+Y), front(-Y), top(+Z), bottom(-Z): the side the camera is on")
    saved: Optional[str] = Field(None, description="A view stored with viewport(save_view=...)")
    zoom: Optional[float] = Field(None, description=">1 closer, <1 farther; applied after the fit")
    margin: Optional[float] = Field(None, description="Fit margin for this view, 0.3-1.0 (1.0 = Apex's tight fit)")


class Scene(BaseModel):
    """What is shown and how. Everything is optional; entities are Apex pathNames from list_tree."""
    show_only: Optional[list[str]] = Field(None, description="Hide everything else, show these (parts, "
                                           "assemblies, solids, surfaces ...)")
    show: Optional[list[str]] = None
    hide: Optional[list[str]] = None
    show_all: bool = False
    hide_all: bool = False
    style: Optional[Style] = None
    highlight: Optional[list[Highlight]] = None
    clear_highlights: bool = False
    labels: Optional[list[Label]] = None
    clear_labels: bool = False
    cut: Optional[Cut] = None


def _scene(scene):
    return scene.model_dump(exclude_none=True) if scene is not None else None


def _view(v):
    return v if v is None or isinstance(v, str) else v.model_dump(exclude_none=True)


@mcp.tool(annotations=LOOKS)
async def viewport(scene: Optional[Scene] = None, view: Union[str, View, None] = None,
                   save_view: Optional[str] = None, delete_view: Optional[str] = None,
                   list_views: bool = False) -> str:
    """Change what Apex's viewport shows and leave it that way (visibility, colours, render style,
    markers, highlights, labels, cut view, camera). `scene` describes what is visible and how it is
    drawn; `view` is a preset name ("iso1", "top", "-y", ...), a saved view, or a View object
    (preset or saved + zoom). Presets are fitted by Apex to what is visible, so show_only first to
    frame one part. For a view no preset gives, set it up in Apex and save_view it. save_view stores the camera under a name (reuse it later as
    view/views entry, also in screenshot), delete_view removes one, list_views lists them. With
    nothing given it just reports the camera. To take a picture without disturbing the user's
    scene use `screenshot` instead."""
    return await _call("viewport", scene=_scene(scene), view=_view(view), save_view=save_view,
                       delete_view=delete_view, list_views=list_views or None)


@mcp.tool(annotations=LOOKS)
async def screenshot(scene: Optional[Scene] = None, views: Optional[list[Union[str, View]]] = None,
                     layout: str = "sheet", crop: str = "model", max_width: Optional[int] = None,
                     margin: Optional[float] = None, restore: bool = True) -> list:
    """Picture(s) of the Apex viewport. Optionally set up a scene first (show_only/hide parts,
    colours, render style, highlights, labels, cut) and take up to 9 views in one call: presets
    ("iso1", "top", "front", ...), saved views, or View objects. The user's scene and camera are
    restored afterwards (restore=true); marker/edge toggles, highlights and cut views cannot be
    read back and are listed under not_restored. layout "sheet" returns one labelled grid for
    several views, "separate" one image each. crop "model" removes Apex's floating panels (model
    browser, scale bar ...) by cropping to the model; "none" returns the whole viewport. Presets
    are zoomed out to `margin` (default 0.85) so the model clears those panels; 1.0 = tight fit.
    Notes: Apex captures SCREEN PIXELS, so its window is raised if something covers it (an
    always-on-top window cannot be raised over and is reported). Parts often share one colour:
    use style.color_by_part to tell them apart. Without arguments it captures what is on screen."""
    res = await _bridge("screenshot", {k: v for k, v in dict(
        scene=_scene(scene), views=[_view(v) for v in views] if views else None, layout=layout,
        crop=crop, max_width=max_width, margin=margin, restore=restore).items() if v is not None})
    paths = [res["sheet"]] if res.get("sheet") else [v["path"] for v in res["views"]]
    return [Image(path=p) for p in paths] + [_json(res)]


# ---------------------------------------------------------------- builders
# Each one CHANGES THE MODEL (confirm with the user first), is idempotent (a repeat call with
# the same name replaces what the earlier one made), and returns ids and counts only.
# Targets: a pathName (whole part/solid/surface), {"path": part, "faces": [face ids]}
# (ids from list_faces), {"path": part, "nodes": [node ids]}, or a list of these.
@mcp.tool(annotations=CHANGES)
async def create_material(name: str, E: float, nu: float, rho: Optional[float] = None,
                          extra: Optional[dict] = None) -> str:
    """Create or update an isotropic MAT1 material in script units (e.g. mm-t-s-N: E in MPa, rho in
    t/mm^3). extra: further MAT1 fields by Apex name. CHANGES THE MODEL."""
    return await _call("create_material", name=name, E=E, nu=nu, rho=rho, extra=extra)


@mcp.tool(annotations=CHANGES)
async def assign_solid_property(paths: list[str], material: str, name: Optional[str] = None) -> str:
    """Create or update a PSOLID (default name PSOLID_<material>) pointing at `material` and assign
    it to every solid of the given parts/assemblies (or the given solids). CHANGES THE MODEL."""
    return await _call("assign_solid_property", paths=paths, material=material, name=name)


@mcp.tool(annotations=CHANGES)
async def mesh_solid(paths: list[str], size: float, order: str = "Quadratic", name: Optional[str] = None) -> str:
    """Tet-mesh the solids of each part (createSolidMesh, order Quadratic | Linear), first deleting
    the meshes already on those solids, so a re-mesh replaces rather than duplicates. Ties to mesh
    nodes of a replaced mesh are lost; ties to faces survive. Returns element/node counts per
    part. CHANGES THE MODEL."""
    return await _call("mesh_solid", paths=paths, size=size, order=order, name=name)


@mcp.tool(annotations=CHANGES)
async def glue(name: str, side1: Any, side2: Any, tolerance: float = 0.5, type: str = "Glue") -> str:
    """Manual interaction pair between two targets (typically faces: {"path": part, "faces": [ids]}),
    with a manual contact tolerance (script length units). type: apex.attribute.InteractionType
    member, default Glue. Needs BCONTACT in the subcases (static_scenario sets it). CHANGES THE MODEL."""
    return await _call("glue", name=name, side1=side1, side2=side2, tolerance=tolerance, type=type)


@mcp.tool(annotations=CHANGES)
async def rbe2(ref: Any, attach: Any, dof: str = "123456", node_part: Optional[str] = None,
               tol: float = 0.01) -> str:
    """Rigid node tie (RBE2) from a reference node to `attach` (faces or nodes). ref is
    {"path": part, "node": id} (e.g. a beam end node from beam) or [x,y,z]: an existing node of
    node_part within tol, else a new node there (node_part is created if missing). A tie already
    on that reference node is replaced. CHANGES THE MODEL."""
    return await _call("rbe2", ref=ref, attach=attach, dof=dof, node_part=node_part, tol=tol)


@mcp.tool(annotations=CHANGES)
async def beam(name: str, points: list[list[float]], radius: float, material: str, mesh_size: float,
               part: str, order: str = "Linear") -> str:
    """Solid-round beam (e.g. bolt shank or pin) along a polyline through `points`, in `part`
    (created if missing): curve, round BeamShape, free BeamSpan with `material`, curve mesh named
    BeamMesh_<name>. Returns nodes_at_points: the mesh node id at each point, ready for rbe2 ref.
    CHANGES THE MODEL."""
    return await _call("beam", name=name, points=points, radius=radius, material=material,
                       mesh_size=mesh_size, part=part, order=order)


@mcp.tool(annotations=CHANGES)
async def constrain(name: str, target: Any, type: str = "Clamped", dof: Optional[str] = None,
                    id: Optional[int] = None) -> str:
    """Displacement constraint (SPC) on a target. type: Clamped | Symmetry | Spherical | Axial |
    General. dof (e.g. "123") fixes only those components through type General; that path is not
    yet proven in Apex, so check the SPC cards with export_bdf. Re-creating keeps the old SPC set
    id unless id is given. CHANGES THE MODEL."""
    return await _call("constrain", name=name, target=target, type=type, dof=dof, id=id)


@mcp.tool(annotations=CHANGES)
async def nodal_load(name: str, target: Any, force: Optional[list[float]] = None,
                     moment: Optional[list[float]] = None, id: Optional[int] = None) -> str:
    """Force [fx,fy,fz] or moment [mx,my,mz] (one per call; each is one Nastran LOAD set) applied
    directly to a target, usually {"path": part, "nodes": [id]}. Re-creating keeps the old LOAD
    set id unless id is given. CHANGES THE MODEL."""
    return await _call("nodal_load", name=name, target=target, force=force, moment=moment, id=id)


@mcp.tool(annotations=CHANGES)
async def static_scenario(name: str, subcases: list[dict], model_rep: Optional[str] = None,
                          bcontact: Any = "auto") -> str:
    """SOL 101 Nastran scenario with one subcase per entry of subcases=[{"name", "load",
    "constraint", "bcontact"?}] (load / constraint by name). Sets LOAD, SPC and BCONTACT per
    subcase and reads them back. bcontact "auto" sets BCONTACT=1 when the model has interactions;
    an int sets that id; null leaves it off. model_rep: assembly path (default: the only top-level
    assembly). Replaces a scenario of the same name. CHANGES THE MODEL."""
    return await _call("static_scenario", name=name, subcases=subcases, model_rep=model_rep,
                       bcontact="off" if bcontact is None else bcontact)


# ------------------------------------------------------------------- skill
# The apex-scripting skill (nastran-fea-skills, cloned into vendor/ by setup.bat) is served
# from here so every MCP client gets it, not only clients that load skills themselves.
_SKILL = {"obj": None, "consulted": False, "reminded": False}
NO_SKILL = ("The apex-scripting skill is not installed, so Apex calls cannot be checked. Run setup.bat "
            "again (it clones %s into vendor/), or set skills_dir in config.json." % SKILLS_REPO)


def _skill():
    if _SKILL["obj"] is None:
        path = find_skill(load_config())
        if path:
            _SKILL["obj"] = ApexScriptingSkill(path)
    return _SKILL["obj"]


@mcp.tool(annotations=READS)
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


@mcp.tool(annotations=READS)
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


@mcp.tool(annotations=READS)
async def apex_describe(target: str = "apex", filter: Optional[str] = None, values: bool = True,
                        limit: int = 200) -> str:
    """What the RUNNING Apex has, read live (the API reference can be wrong: moved classes, enum
    members it leaves out, undocumented functions), merged with the reference's signatures.
    target:
      * an API path: 'apex.mesh' (module: its functions, classes, enums; * marks names the
        reference lacks), 'apex.mesh.createSolidMesh' (runtime and documented signature),
        'apex.mesh.SurfaceMeshElementShape' (enum members with values), 'apex.Part' (class:
        properties and methods), 'apex.Part.update' (method signature);
      * an entity pathName from list_tree: its class, property values and methods;
      * '$name' or '$name.attr': a variable left by run_apex_python (e.g. '$part', '$res.meshes').
    filter keeps member names containing that text. values=False lists an object's properties
    without reading them. Properties that can hold every element or node (elements, nodes,
    ...entities, ...IDs) are never read. Read-only."""
    res = await _bridge("describe", {"target": target, "filter": filter, "values": values, "limit": limit})
    _SKILL["consulted"] = True  # grounded in the live API, like a lookup
    return format_describe(res, _skill(), PF.load_overrides())


# --------------------------------------------------------------------- raw
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


def format_script_result(res, show_log=False):
    """Plain text for run_apex_python: stdout, then result=<compact JSON>, then what the script
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
    if failed or show_log:
        parts.append("[%s after %ss; script saved to %s]"
                     % ("error" if failed else "ok", res.get("seconds"), res.get("logged_to")))
    return "\n".join(parts) or "(ran, no output: print() or set result = ... to return something)"


def call_signatures(names, limit=3):
    """API-reference signature lines for qualified function names ('apex.getEntities') or the
    worker's call records ({'kind': 'method', 'class': 'Part', 'method': 'update', ...})."""
    sk = _skill()
    if sk is None:
        return []
    out = []
    for n in names[:limit]:
        if isinstance(n, dict) and n.get("kind") == "method":
            sigs = sk.method_signature(n["class"], n["method"], n.get("bases", ()))
            missing = ("%s: not in the API reference (apex_api_lookup('%s') lists what the class documents)"
                       % (n["name"], n["class"]))
        else:
            label = n["name"] if isinstance(n, dict) else n
            sigs = sk.signature(label)
            missing = "%s: not in the API reference" % label
        out += sigs or [missing]
    return out


_LIVE = {"index": None, "state": "untried"}  # untried | ok | unsupported


async def live_api_index():
    """The running Apex's own API names (worker/introspect.py), fetched once per server session.
    None when the bridge's worker is too old to have api_index; preflight then uses the docs."""
    if _LIVE["state"] == "untried":
        try:
            res = await _bridge("api_index", {})
            with open(res["path"], "r", encoding="utf-8") as fh:
                _LIVE["index"] = json.load(fh)
            _LIVE["state"] = "ok"
        except ToolError as exc:  # bridge down or busy: try again next time; anything else: docs only
            if not any(m in str(exc) for m in ("Cannot reach", "busy", "Lost connection", "timed out", "did not answer")):
                _LIVE["state"], _LIVE["error"] = "unsupported", str(exc)[:300]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _LIVE["state"], _LIVE["error"] = "unsupported", str(exc)[:300]
    return _LIVE["index"]


async def run_preflight(code, mode="run"):
    sk = _skill()
    live = await live_api_index()
    return PF.check(code, sk.doc_index() if sk is not None else None, live, mode=mode), live is not None


def preflight_refusal(pf, live, mode="run"):
    errs = pf["errors"]
    names = []
    for e in errs:
        if e.get("name") and e["name"] not in names and "argument" in e["msg"]:
            names.append(e["name"])
    text = "Not run: preflight found %d problem(s) in the script:\n%s" % (len(errs), PF.format_issues(errs))
    sigs = call_signatures(names)
    if sigs:
        text += "\n\nAPI reference:\n" + "\n".join("  " + x for x in sigs)
    if pf["warnings"]:
        text += "\n\nWarnings:\n" + PF.format_issues(pf["warnings"])
    text += "\n\nNothing was sent to Apex. Fix the script and resend it."
    if any(e.get("query") for e in errs):
        text += (" The read-only checks cannot be forced: a script that changes the model or the scene, "
                 "writes a file or starts a process belongs in run_apex_python.")
    else:
        text += (" If a check is wrong (Apex has the name or accepts the call), resend with force=True. "
                 "apex_describe('apex.<module>') lists what this Apex has.")
    if not live:
        text += " Names were checked against the API reference only: the bridge did not provide a live index."
    return text


def effects_changed(eff):
    """True when an effects receipt shows a change (not just timing or untracked sections)."""
    return isinstance(eff, dict) and any(k in eff for k in ("created", "deleted", "changed", "model_switched"))


async def _run_script(code, mode, attach=True, show_log_path=False, force=False, effects=True):
    """run_apex_python and query_apex_python: skill gate, preflight, run, then the result text."""
    cfg = load_config()
    sk = _skill()
    query = mode == "query"
    if sk is not None and cfg.get("require_skill_lookup", True) and not _SKILL["consulted"]:
        raise ToolError("Not run. Read apex_scripting_guide('scripting-model') first, and check the "
                        "apex calls in this script with apex_api_lookup, then resend it. (Enforced once per "
                        "session; set require_skill_lookup to false in config.json to turn it off.)")
    pf = None
    if cfg.get("script_preflight", True) or query:  # the read-only check is what makes a query a query
        pf, live = await run_preflight(code, mode)
        if pf["errors"] and (not force or any(e.get("query") for e in pf["errors"])):
            raise ToolError(preflight_refusal(pf, live, mode))
    track = bool(effects and cfg.get("script_effects", True))  # the worker snapshots a query regardless
    res = await _bridge("run_apex_python", {"code": code, "attach": attach or query, "effects": track, "mode": mode})
    out = format_script_result(res, show_log_path)
    eff = res.get("effects") if isinstance(res, dict) else None
    if query and effects_changed(eff):  # the static check missed something: say so first, loudly
        out = ("WARNING: this query CHANGED THE MODEL (not undone; see effects= below). The read-only check "
               "missed it: tell the user what changed. Use run_apex_python for scripts that change things.\n\n" + out)
    elif query and isinstance(eff, dict) and eff.get("error"):
        out += "\n(could not confirm that the query changed nothing: %s)" % eff["error"]
    err = res.get("error") if isinstance(res, dict) else None
    if err and err.get("calls"):
        sigs = call_signatures(err["calls"])
        if sigs:
            out += "\n\nAPI reference for the failing call%s:\n%s" % (
                "s" if len(sigs) > 1 else "", "\n".join("  " + x for x in sigs))
    if pf is not None and (pf["warnings"] or (force and pf["errors"])):
        notes = (pf["errors"] if force else []) + pf["warnings"]
        out += "\n\npreflight%s:\n%s" % (" (forced past)" if force and pf["errors"] else " warnings",
                                         PF.format_issues(notes))
    if sk is None:
        return "NOTE: %s\n\n%s" % (NO_SKILL, out)
    # script errors come back as a result with a traceback, not as a ToolError
    if isinstance(res, dict) and res.get("traceback") and not _SKILL["reminded"]:
        _SKILL["reminded"] = True  # once per session
        out += ("\n\nThe script raised. Before retrying, look up the failing call with apex_api_lookup "
                "(and apex_scripting_guide('pitfalls')) instead of guessing a new signature.")
    return out


SCRIPT_RULES = """
    MEMORY RULES - Apex shares this process with the user's model:
    * Never iterate part.elements / part.nodes / mesh.elements / mesh.nodes, and never read
      per-element attributes (material, elementProperty2D, ...) across a whole mesh.
    * Use bulk arrays: mesh.getElementIDs() {topology: ids}, mesh.getElementConnectivities(),
      mesh.getNodeIDs(), mesh.getNodeCoordinates() (flat xyz) - with numpy, which Apex ships.
    * Aggregate in the script; return counts / min / max / top-N, not raw lists.
    * Helpers preloaded as Q (unless the script binds Q itself; `import queries as Q` brings it
      back): Q.part_summary(part), Q.mesh_summary(mesh, sample=3), Q.sample_elements(mesh, n),
      Q.topology_counts(mesh), Q.solid_summary(solid), Q.surface_summary(surf), Q.bbox(entity),
      Q.Budget(seconds) (`if b.over(): break`), Q.faces_by_id(part, ids) -> EntityCollection,
      Q.nodes_on_face(face_or_faces, ids_only=False), Q.nodes_where(mesh_or_part, fn) with
      fn(xyz Nx3 numpy) -> bool mask, Q.nodes_by_id(part, ids), Q.node_near(part, xyz, tol),
      Q.rle(ids) -> '1-3,7-7' for getNodes/getElements(ids=...), Q.face_shape(face).

    API RULES - generated Apex code usually fails on these:
    * Pass arguments by keyword: Apex also accepts positional ones, but keywords are what the
      API reference documents and what preflight checks.
    * Change entity properties with entity.update(...); plain assignment raises AttributeError.
    * Pass targets as apex.EntityCollection (appendList).
    * Confirm calls, enum members and properties with apex_api_lookup or apex_describe first. The
      first script of a session is refused until one of apex_scripting_guide, apex_api_lookup or
      apex_describe has been used."""


QUERY_DOC = """READ-ONLY Python on Apex's script thread, for anything the summary tools do not cover:
    counting, measuring, collecting ids, checking properties. Same namespace, output and preflight
    as run_apex_python (variables persist between the two), but the script is refused before it
    runs if it would change anything:
    * an apex function that is not a read (getX, isX, asX, evaluateX, measureX, findX ... are
      reads; createX, deleteEntities, setX, assignX, display.render, undo ... are not);
    * a method that changes an entity or the scene (update, setX, createX, removeX, hide, show,
      highlight, save ...), except on a dict the script builds itself ({...}, dict());
    * writing a file (open(.., 'w'), np.save, Path.write_text ...), os.remove / os.system, imports
      of subprocess, shutil, socket, ctypes ..., and exec / eval / setattr.
    Adding to an EntityCollection or a list is fine. The model is snapshotted before and after: if
    the script changed it anyway, the result starts with a WARNING and the effects. force=True gets
    past the ordinary preflight errors, never the read-only ones. To change the model use
    run_apex_python; for the view use viewport / screenshot.
""" + SCRIPT_RULES


@mcp.tool(annotations=READS, description=QUERY_DOC)
async def query_apex_python(code: str, show_log_path: bool = False, force: bool = False) -> str:
    return await _run_script(code, "query", show_log_path=show_log_path, force=force)


RUN_DOC = """Run Python on Apex's script thread that CHANGES the model or the scene; for scripts that
    only read, use query_apex_python (refused if it would change anything, so it is safe to allow).
    Variables persist between calls of both. Returns plain text: print() output (last 20k chars),
    then `result=<JSON>` if the script set `result = ...`, then `effects=` (what the script
    created, deleted or changed in the model: parts, assemblies, solid/surface/curve/mesh/element/
    node counts, loads, constraints, connections, materials, properties, scenarios; `effects: none`
    when it changed none of these), then on failure the script's traceback, the API signature of
    the failing apex call and where the script was saved (every script is saved to output/scripts;
    show_log_path=True reports the path on success too).

    PREFLIGHT: the script is checked before anything is sent to Apex, without running it. It is
    refused (nothing runs) for a syntax error, an apex name or enum member this Apex does not
    have, a keyword argument the function does not take or a missing required one, and a loop
    over .elements / .nodes. Other findings come back as warnings with the output. force=True
    runs a refused script anyway. effects=False skips the model snapshots before and after (about
    50 ms on a small model, capped by effects_budget_s on a large one).

    Model changes: confirm with the user first. The builder tools (create_material, mesh_solid,
    glue, rbe2, beam, constrain, nodal_load, static_scenario, ...) cover the common steps.
""" + SCRIPT_RULES


@mcp.tool(annotations=CHANGES, description=RUN_DOC)
async def run_apex_python(code: str, attach: bool = True, show_log_path: bool = False, force: bool = False,
                          effects: bool = True) -> str:
    return await _run_script(code, "run", attach, show_log_path, force, effects)


def main():
    try:
        mcp.run()
    finally:
        BRIDGE.stop()


if __name__ == "__main__":
    sys.exit(main())
