"""Viewport scenes: what is visible, how it is drawn, where the camera is - and pictures of it.

Two tools are built on one scene applicator:
  viewport(scene, view, ...)      applies a scene and leaves it applied; also saves/lists named views.
  screenshot(scene, views, ...)   applies a scene, captures each view, restores the user's scene.

What was measured in Apex 2024.1 (and shaped this file)
-------------------------------------------------------
* apex.display.captureImage grabs SCREEN PIXELS of Apex's window. A window on top of Apex (an
  Explorer window, a dialog) ends up in the picture, so before every capture the Apex window is
  checked for coverage (Win32 WindowFromPoint) and raised if needed.
* With Apex's UI hidden it returns a path and writes a 0-byte file without raising, so every
  capture is validated and retried.
* The floating panels (model browser, mechanical-system bar, scale bar) are part of the grab.
  They are removed by differencing against a frame taken with every part hidden.
* Visibility is a tri-state (Hidden 0, Visible 1, NotSet -1) and show()/hide() on a container
  cascade to its children. Scenes therefore change visibility on parts and entities only, never
  session.hideAll(), and restore parts first, then their children.
* Camera: viewFrom*/viewIsometric_* with reset=True fit the visible scene and centre it, and
  Camera.zoom() is reliable. Everything else is not: the camera's coordinates and parallel scale
  are not model units (focal points of 1e6, rotating about the focal point throws a part out of
  frame, setParallelScale and pan() move things unpredictably), so no framing maths, rotation or
  panning is done here. Custom views are a preset plus zoom, or a saved camera, which is restored
  in the order position, focal point, view-up, scale (verified pixel-exact).
* A tight fit runs into the floating panels, so screenshots zoom out to margin 0.85 by default.
* No Apex API reads back the marker/edge display toggles, highlights or cut views, so a
  screenshot cannot undo those; it reports them under "not_restored".

Everything is keyword-only, like the rest of the Apex API. No handler walks elements or nodes.
"""
import datetime
import json
import math
import os
import re
import time

MAX_VIEWS = 9
MIN_MODEL_PIXELS = 1500  # changed pixels a block needs to count as the model (calibrated live, see model_box)
MAX_CHILDREN = 4000  # visibility snapshot of part children (solids/surfaces/meshes/curves)
Z_UP = [0.0, 0.0, 1.0]
Y_UP = [0.0, 1.0, 0.0]

# name -> (Camera method, reverse, viewUp). Apex's "direction of projection" points from the
# focal point to the camera, so viewFromZ() is a view from +Z (top) and reverse=True from -Z.
PRESETS = {
    "iso1": ("viewIsometric_1", False, Z_UP), "iso2": ("viewIsometric_2", False, Z_UP),
    "iso3": ("viewIsometric_3", False, Z_UP), "iso4": ("viewIsometric_4", False, Z_UP),
    "x": ("viewFromX", False, Z_UP), "-x": ("viewFromX", True, Z_UP),
    "y": ("viewFromY", False, Z_UP), "-y": ("viewFromY", True, Z_UP),
    "z": ("viewFromZ", False, Y_UP), "-z": ("viewFromZ", True, Y_UP),
}
PRESETS.update({"right": PRESETS["x"], "left": PRESETS["-x"], "back": PRESETS["y"],
                "front": PRESETS["-y"], "top": PRESETS["z"], "bottom": PRESETS["-z"]})
PRESET_HELP = "iso1-iso4, x, -x, y, -y, z, -z, right(+X), left(-X), back(+Y), front(-Y), top(+Z), bottom(-Z)"

RENDER_NAMES = {0: "Wireframe", 1: "HiddenLines", 2: "Filled", 3: "FilledWithEdges", 4: "Shaded",
                5: "ShadedWithEdges"}
PALETTE = [[230, 159, 0], [86, 180, 233], [0, 158, 115], [240, 228, 66], [0, 114, 178],
           [213, 94, 0], [204, 121, 167], [153, 153, 153]]  # Okabe-Ito: distinct, colour-blind safe
NAME_OK = re.compile(r"^[A-Za-z0-9_. -]{1,60}$")


def _Q(W):
    return W.Q


# ---------------------------------------------------------------- small helpers
def _int(v, default=None):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return default


def _vis(e):
    """1 visible, 0 hidden, -1 not set / mixed, None if the entity has no visibility."""
    try:
        s = str(e.getVisibility())
    except Exception:
        return None
    n = _int(s)
    if n is not None:
        return n
    return 0 if "Hidden" in s else 1 if "Visible" in s else -1


def _set_vis(e, v):
    if v == 1:
        e.show()
    elif v == 0:
        e.hide()


def _rgb(c, what):
    try:
        r, g, b = [int(x) for x in c]
    except Exception:
        raise ValueError("%s must be [r, g, b]" % what)
    if not all(0 <= x <= 255 for x in (r, g, b)):
        raise ValueError("%s values must be 0-255" % what)
    return [r, g, b]


def _parts(W):
    return list(W.ensure_apex().currentModel().getParts(recursive=True))


def _children(W, part):
    out = []
    for getter in ("getSolids", "getSurfaces", "getMeshes", "getCurves"):
        coll = _Q(W).safe(lambda g=getter: getattr(part, g)())
        if coll is not None:
            out.extend(list(coll))
    return out


# ---------------------------------------------------------------- visibility state
def read_vis(W, children=True):
    """Visibility of every assembly and part and (optionally) of the entities inside the parts."""
    asms = _Q(W).safe(lambda: list(W.ensure_apex().currentModel().getAssemblies(recursive=True))) or []
    assemblies = [(a, _vis(a)) for a in asms]
    parts = [(p, _vis(p)) for p in _parts(W)]
    kids, truncated = [], False
    if children:
        for p, _ in parts:
            for c in _children(W, p):
                v = _vis(c)
                if v is None:
                    continue
                if len(kids) >= MAX_CHILDREN:
                    truncated = True
                    break
                kids.append((c, v))
    return {"assemblies": assemblies, "parts": parts, "children": kids, "truncated": truncated}


def write_vis(state):
    """Restore a read_vis() state: assemblies with a definite value first, outermost first (they
    cascade to everything below), then parts, then the children that still differ.
    Only differing values are written: setting a child explicitly turns its parent's flag into
    NotSet (-1), so writing every child back would change parts that were plain Visible. A
    container that was NotSet is left alone: it follows from its contents."""
    for a, v in sorted(state["assemblies"], key=lambda t: Q_path(t[0]).count("/")):
        if v in (0, 1) and _vis(a) != v:
            _set_vis(a, v)
    for p, v in state["parts"]:
        if _vis(p) != v:
            _set_vis(p, v)
    for c, v in state["children"]:
        if _vis(c) != v:
            _set_vis(c, v)


# ---------------------------------------------------------------- camera
def _xyz(v):
    return [float(v.x), float(v.y), float(v.z)]


def camera_state(cam):
    return {"position": _xyz(cam.getPosition()), "focal_point": _xyz(cam.getFocalPoint()),
            "view_up": _xyz(cam.getViewUp()), "parallel_scale": float(cam.getParallelScale())}


def set_camera(W, cam, st):
    """Order matters: position, then focal point, then view-up, then scale (verified pixel-exact)."""
    apex = W.ensure_apex()
    cam.setPosition(position=apex.Coordinate(*st["position"]))
    cam.setFocalPoint(focalPoint=apex.Coordinate(*st["focal_point"]))
    u = st["view_up"]
    cam.setViewUp(viewUp=apex.construct.Vector3D(x=u[0], y=u[1], z=u[2]))
    cam.setParallelScale(parallelScale=float(st["parallel_scale"]))


def _fit(cam, preset):
    method, reverse, up = PRESETS[preset]
    fn = getattr(cam, method)
    try:
        fn(reverse=reverse, viewUp=list(up), reset=True)
    except TypeError:  # an Apex build without the viewUp argument
        fn(reverse=reverse)


def views_dir(W):
    return W.out_dir("views")


def _view_file(W, name):
    return os.path.join(views_dir(W), name + ".json")


def list_saved(W):
    out = {}
    for f in sorted(os.listdir(views_dir(W))):
        if f.endswith(".json"):
            try:
                with open(os.path.join(views_dir(W), f), "r", encoding="utf-8") as fh:
                    out[f[:-5]] = json.load(fh).get("saved", "")
            except Exception:
                pass
    return out


def save_view(W, name, cam):
    if not NAME_OK.match(name or ""):
        raise ValueError("view name must be 1-60 characters of letters, digits, space, '_', '-', '.'")
    st = camera_state(cam)
    with open(_view_file(W, name), "w", encoding="utf-8") as fh:
        json.dump({"saved": datetime.datetime.now().isoformat(timespec="seconds"), "camera": st}, fh)
    return st


def load_view(W, name):
    path = _view_file(W, name) if NAME_OK.match(name or "") else None
    if not path or not os.path.isfile(path):
        raise KeyError("no saved view '%s'. Saved: %s. Presets: %s" % (name, sorted(list_saved(W)), PRESET_HELP))
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["camera"]


def resolve_view(W, v):
    """A view given as a string or an object -> {'name', 'preset'|'saved', relative ops}."""
    if v is None:
        return {"name": "current"}
    spec = {"name": v, "preset": None} if isinstance(v, str) else dict(v)
    if isinstance(v, str):
        key = v.strip().lower()
        if key in PRESETS:
            spec = {"name": v, "preset": key}
        elif NAME_OK.match(v) and os.path.isfile(_view_file(W, v)):
            spec = {"name": v, "saved": v}
        else:
            raise KeyError("unknown view '%s'. Presets: %s. Saved: %s" % (v, PRESET_HELP, sorted(list_saved(W))))
        return spec
    if spec.get("preset"):
        key = str(spec["preset"]).strip().lower()
        if key not in PRESETS:
            raise KeyError("unknown preset '%s'. Presets: %s" % (spec["preset"], PRESET_HELP))
        spec["preset"] = key
    if spec.get("saved"):
        load_view(W, spec["saved"])  # raises if missing
    if spec.get("preset") and spec.get("saved"):
        raise ValueError("a view takes either preset or saved, not both")
    spec["name"] = spec.get("name") or spec.get("saved") or spec.get("preset") or "current"
    return spec


def aim(W, cam, spec, margin=1.0):
    """Point the camera as `spec` says and return its state. A preset is fitted by Apex to what is
    visible and then zoomed out by `margin` (<1) so the model stays clear of the floating panels."""
    if spec.get("saved"):
        set_camera(W, cam, load_view(W, spec["saved"]))
        factor = float(spec.get("zoom") or 1.0)
    elif spec.get("preset"):
        _fit(cam, spec["preset"])
        factor = float(spec.get("zoom") or 1.0) * float(spec.get("margin") or margin)
    else:  # relative to the current camera
        factor = float(spec.get("zoom") or 1.0)
    if abs(factor - 1.0) > 1e-9:
        cam.zoom(factor=factor)
    return camera_state(cam)


# ---------------------------------------------------------------- scene validation
def validate(W, spec, views=()):
    """Check everything up front so a bad request changes nothing. Returns {pathName: entity}."""
    apex = W.ensure_apex()
    if spec.get("show_only") and (spec.get("show_all") or spec.get("hide_all")):
        raise ValueError("show_only cannot be combined with show_all / hide_all")
    st = spec.get("style") or {}
    paths = set(spec.get("show_only") or []) | set(spec.get("show") or []) | set(spec.get("hide") or [])
    paths |= set((st.get("transparency") or {})) | set((st.get("color") or {})) | set(st.get("paths") or [])
    for h in spec.get("highlight") or []:
        paths |= set(h.get("paths") or [])
        if h.get("part_path"):
            paths.add(h["part_path"])
        if not (h.get("paths") or (h.get("part_path") and h.get("ids"))):
            raise ValueError("highlight needs paths, or part_path + ids")
    ents, missing = {}, []
    for p in sorted(paths):
        try:
            ents[p] = W.entities_by_path([p])[0]
        except Exception:
            missing.append(p)
    if missing:
        raise KeyError("not found: %s (use list_tree / find_entities for exact pathNames)" % missing)
    if st.get("render_style"):
        W.enum(apex.session.DisplayRenderStyle, st["render_style"], "render_style")
    for k, c in (st.get("color") or {}).items():
        _rgb(c, "color of %s" % k)
    for k, t in (st.get("transparency") or {}).items():
        if not 0 <= int(t) <= 100:
            raise ValueError("transparency of %s must be 0-100" % k)
    for h in spec.get("highlight") or []:
        if h.get("color"):
            _rgb(h["color"], "highlight color")
    if len(views) > MAX_VIEWS:
        raise ValueError("at most %d views per call" % MAX_VIEWS)
    return ents


# ---------------------------------------------------------------- scene session
class Scene:
    """Applies a scene spec; remembers enough to put the user's scene back."""

    def __init__(self, W, spec, ents):
        self.W, self.spec, self.ents = W, spec or {}, ents
        self.apex = W.ensure_apex()
        self.vis0 = None
        self.style0 = []     # (entity, original style dict)
        self.labels = []     # GraphicsText objects we created
        self.highlighted = False
        self.cut = False
        self.globals = []    # display toggles that cannot be read back
        self.applied = []
        self.warnings = []

    # -- snapshot
    def snapshot(self):
        sp = self.spec
        if sp.get("show_only") or sp.get("show") or sp.get("hide") or sp.get("show_all") or sp.get("hide_all"):
            self.vis0 = read_vis(self.W)
            if self.vis0["truncated"]:
                self.warnings.append("more than %d entities inside parts: restore covers parts and the first "
                                     "%d entities inside them" % (MAX_CHILDREN, MAX_CHILDREN))
        st = sp.get("style") or {}
        targets = []
        if st.get("render_style"):
            targets += [self.ents[p] for p in st.get("paths") or []] or _parts(self.W)
        targets += [self.ents[p] for p in (st.get("transparency") or {})]
        targets += [self.ents[p] for p in (st.get("color") or {})]
        if st.get("color_by_part"):
            targets += _parts(self.W)
        seen = set()
        for e in targets:
            if id(e) in seen:
                continue
            seen.add(id(e))
            self.style0.append((e, self._read_style(e)))

    @staticmethod
    def _read_style(e):
        s = {}
        for key, fn in (("color", "getColor"), ("render", "getDisplayRenderStyle"),
                        ("transp", "getDisplayTransparency")):
            try:
                v = getattr(e, fn)()
                s[key] = [int(c) for c in v] if key == "color" else _int(v)
            except Exception:
                s[key] = None
        return s

    # -- apply
    def apply(self):
        apex, W, sp = self.apex, self.W, self.spec
        parts = None
        if sp.get("hide_all") or sp.get("show_only"):
            parts = _parts(W)
            for p in parts:
                p.hide()
            self.applied.append("hide_all" if sp.get("hide_all") else "show_only")
        if sp.get("show_all"):
            for p in _parts(W):
                p.show()
            self.applied.append("show_all")
        for path in list(sp.get("show_only") or []) + list(sp.get("show") or []):
            self.ents[path].show()
        for path in sp.get("hide") or []:
            self.ents[path].hide()
        if sp.get("show") or sp.get("hide"):
            self.applied.append("show/hide")
        self._apply_style(sp.get("style") or {})
        self._apply_highlight()
        self._apply_labels()
        self._apply_cut()
        apex.display.render()
        return self.applied

    def _update(self, e, what, **kw):
        try:
            e.update(**kw)
        except Exception as exc:
            self.warnings.append("%s on %s failed: %s" % (what, Q_path(e), str(exc)[:120]))

    def _apply_style(self, st):
        apex, W = self.apex, self.W
        if st.get("render_style"):
            member = W.enum(apex.session.DisplayRenderStyle, st["render_style"], "render_style")
            for e in ([self.ents[p] for p in st.get("paths") or []] or _parts(W)):
                self._update(e, "render_style", renderStyle=member)
            self.applied.append("render_style")
        colors = {}
        if st.get("color_by_part"):
            vis = [p for p in _parts(W) if _vis(p) != 0]
            for i, p in enumerate(vis):
                colors[id(p)] = (p, PALETTE[i % len(PALETTE)])
        for path, c in (st.get("color") or {}).items():
            colors[id(self.ents[path])] = (self.ents[path], _rgb(c, "color"))
        for e, c in colors.values():
            self._update(e, "color", color=c)
        if colors:
            self.applied.append("color")
        for path, t in (st.get("transparency") or {}).items():
            self._update(self.ents[path], "transparency", enableTransparency=W.apex_bool(int(t) > 0),
                         transparencyLevel=int(t))
            self.applied.append("transparency")
        for key, fn, arg in (("connection_markers", "displayConnectionMarkers", "display"),
                             ("loads_bc_markers", "displayLoadsAndBCMarkers", "display"),
                             ("mesh_edges", "displayMeshTopologyEdges", "displayEdges")):
            if st.get(key) is not None:
                getattr(apex.session, fn)(**{arg: bool(st[key])})
                self.globals.append(key)
                self.applied.append(key)

    def _apply_highlight(self):
        apex, W, sp = self.apex, self.W, self.spec
        if sp.get("clear_highlights"):
            apex.session.clearHighlights()
            self.applied.append("clear_highlights")
        for h in sp.get("highlight") or []:
            rgb = _rgb(h.get("color") or [255, 0, 255], "highlight color")
            color = apex.ColorRGB(*rgb)
            if h.get("ids"):
                ids = [int(i) for i in h["ids"]]
                cap = int(W.cfg("max_highlight_ids"))
                if len(ids) > cap:
                    raise ValueError("%d ids requested; max_highlight_ids is %d" % (len(ids), cap))
                coll = apex.get(target=[{"type": h.get("kind", "Element"), "path": h["part_path"],
                                         "ids": _Q(W).rle(ids)}])
            else:
                coll = W.entities_by_path(h["paths"])
            for e in coll:
                e.highlight(colorRGB=color)
            self.highlighted = True
            self.applied.append("highlight")

    def _apply_labels(self):
        apex, sp = self.apex, self.spec
        if sp.get("clear_labels"):
            apex.display.clearAllGraphicsText()
            self.applied.append("clear_labels")
        for lb in sp.get("labels") or []:
            x, y, z = [float(c) for c in lb["at"]]
            rgb = _rgb(lb.get("color") or [0, 0, 0], "label color")
            self.labels.append(apex.display.displayText(
                text=lb["text"], textLocation=apex.Coordinate(x, y, z),
                graphicsFontColor=apex.ColorRGB(*rgb), graphicsFontSize=int(lb.get("size", 14))))
            self.applied.append("label")

    def _apply_cut(self):
        apex, cut = self.apex, self.spec.get("cut")
        if not cut:
            return
        if cut.get("off"):
            apex.display.displayCutViews(display=False)
            self.applied.append("cut off")
            return
        kw = {"bSlideMode": bool(cut.get("slice", False))}
        if cut.get("location") is not None:
            x, y, z = [float(c) for c in cut["location"]]
            kw["location"] = apex.Coordinate(x, y, z)
        apex.display.createCutView(**kw)
        apex.display.displayCutViews(display=True)
        self.cut = True
        self.applied.append("cut")

    # -- restore
    def restore(self):
        """Put back what can be read back. Returns the list of things that cannot be."""
        apex, W = self.apex, self.W
        problems = []
        if self.vis0 is not None:
            try:
                write_vis(self.vis0)
            except Exception as exc:
                problems.append("visibility: %s" % str(exc)[:120])
        for e, s in self.style0:
            kw = {}
            if s.get("color"):
                kw["color"] = s["color"]
            if s.get("render") in RENDER_NAMES:
                kw["renderStyle"] = getattr(apex.session.DisplayRenderStyle, RENDER_NAMES[s["render"]])
            if s.get("transp") is not None:
                kw["enableTransparency"] = W.apex_bool(s["transp"] > 0)
                kw["transparencyLevel"] = s["transp"]
            if kw:
                try:
                    e.update(**kw)
                except Exception as exc:
                    problems.append("style of %s: %s" % (Q_path(e), str(exc)[:100]))
            if s.get("render") not in RENDER_NAMES and (self.spec.get("style") or {}).get("render_style"):
                try:
                    e.unsetRender()
                except Exception:
                    pass
        for t in self.labels:
            try:
                apex.display.clearText(graphicsText=t)
            except Exception as exc:
                problems.append("label: %s" % str(exc)[:80])
        if self.highlighted:
            try:
                apex.session.clearHighlights()
            except Exception:
                pass
        notes = []
        if self.globals:
            notes.append("display toggles %s (Apex cannot read them back)" % self.globals)
        if self.cut:
            notes.append("cut view (hidden again with cut={'off': true})")
            try:
                apex.display.displayCutViews(display=False)
            except Exception:
                pass
        return problems + notes


def Q_path(e):
    try:
        return e.getPathName()
    except Exception:
        return str(e)


# ---------------------------------------------------------------- window guard (Win32, Apex process)
def _win_probe():
    """State of Apex's main window, or None where it cannot be inspected (not Windows, no window)."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    except Exception:
        return None
    pid = k32.GetCurrentProcessId()
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def title(h):
        n = u32.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, b, n + 1)
        return b.value

    def owner(h):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        return p.value

    def rect(h):
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    found = []

    def cb(h, _l):
        if u32.IsWindowVisible(h) and owner(h) == pid and not u32.GetWindow(h, 4):  # GW_OWNER: top level only
            r = rect(h)
            found.append(((r[2] - r[0]) * (r[3] - r[1]), h))
        return True

    u32.EnumWindows(enum_proc(cb), 0)
    if not found:
        return None
    main = max(found)[1]
    iconic = bool(u32.IsIconic(main))
    covered = []
    if not iconic:
        left, top, right, bottom = rect(main)
        for fx in (0.3, 0.5, 0.7):
            for fy in (0.35, 0.55, 0.75):
                w = u32.WindowFromPoint(wt.POINT(int(left + (right - left) * fx), int(top + (bottom - top) * fy)))
                root = u32.GetAncestor(w, 2)  # GA_ROOT
                if root and owner(root) != pid:
                    covered.append(title(root)[:60] or "(untitled window)")
    return {"hwnd": main, "iconic": iconic, "covered_by": sorted(set(covered))}


def _win_raise(hwnd):
    """Bring Apex in front of ordinary windows without taking keyboard focus (topmost toggle)."""
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.windll.user32
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    u32.SetWindowPos(hwnd, wt.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
    u32.SetWindowPos(hwnd, wt.HWND(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST


def ensure_visible(W):
    """Raise if Apex's window is covered or minimized and cannot be brought forward."""
    info = _win_probe()
    if info is None:
        return {"checked": False}
    raised = False
    if info["iconic"] or info["covered_by"]:
        if not W.CFG.get("screenshot_bring_to_front", True):
            raise RuntimeError("Apex's window is %s and screenshot_bring_to_front is false"
                               % ("minimized" if info["iconic"] else "covered by %s" % info["covered_by"]))
        _win_raise(info["hwnd"])
        raised = True
        time.sleep(0.4)
        info = _win_probe() or info
    if info["iconic"] or info["covered_by"]:
        raise RuntimeError("Apex's window is %s. The screenshot grabs screen pixels, so nothing may cover Apex: "
                           "close or move that window (an always-on-top window cannot be raised over)."
                           % ("minimized" if info["iconic"] else "covered by %s" % info["covered_by"]))
    return {"checked": True, "brought_to_front": raised}


# ---------------------------------------------------------------- capture, crop, sheet
def _pil():
    try:
        from PIL import Image, ImageChops, ImageDraw
        return Image, ImageChops, ImageDraw
    except Exception:
        return None, None, None


def capture(W, tag):
    """One validated viewport capture -> (path, PIL image or None). Retries empty/unreadable files."""
    apex = W.ensure_apex()
    folder = W.out_dir("screenshots")
    Image = _pil()[0]
    last = "no file"
    for attempt in range(3):
        apex.display.render()
        prefix = "%s_%s" % (tag, W.stamp())
        returned = apex.display.captureImage(path=folder, imageNamePrefix=prefix,
                                             imageCaptureRegion=apex.display.CaptureRegionType.Viewport,
                                             imageFormat=apex.display.ImageType.png)
        path = returned if returned and os.path.isfile(returned) else None
        if path is None:
            cands = sorted(f for f in os.listdir(folder) if f.startswith(prefix))
            path = os.path.join(folder, cands[-1]) if cands else None
        if path and os.path.getsize(path) > 0:
            if Image is None:
                return path, None
            try:
                im = Image.open(path)
                im.load()
                return path, im.convert("RGB")
            except Exception as exc:
                last = "unreadable image (%s)" % exc
        else:
            last = "empty file" if path else "no file written"
        time.sleep(0.25)
    raise RuntimeError("Apex's captureImage produced %s three times. Is Apex's UI hidden, the window minimized "
                       "or a modal dialog open?" % last)


def model_box(ref, im, cell=24, thresh=10, min_frac=0.04, min_pixels=MIN_MODEL_PIXELS):
    """Bounding box (x0, y0, x1, y1) of what the model added to the empty-scene frame `ref`, or
    None. Small stray differences (triad, scale bar, tree check marks) are ignored: only the
    largest connected block of changed cells counts, and only if it holds at least `min_pixels`
    changed pixels (an empty scene differs from its reference only by such strays)."""
    _, ImageChops, _ = _pil()
    if ref is None or im is None or ref.size != im.size:
        return None
    diff = ImageChops.difference(ref.convert("L"), im.convert("L")).point(lambda v: 255 if v > thresh else 0)
    small = diff.reduce(cell)
    gw, gh = small.size
    px = small.load()
    cells = {(x, y): px[x, y] for y in range(gh) for x in range(gw) if px[x, y] > 255 * min_frac}
    best, best_w, seen = None, 0, set()
    for start in cells:
        if start in seen:
            continue
        stack, comp, wsum = [start], [], 0
        seen.add(start)
        while stack:
            c = stack.pop()
            comp.append(c)
            wsum += cells[c]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (c[0] + dx, c[1] + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        if wsum > best_w:
            best, best_w = comp, wsum
    if not best or best_w / 255.0 * cell * cell < min_pixels:
        return None
    xs = [c[0] for c in best]
    ys = [c[1] for c in best]
    x0, x1 = min(xs) * cell, (max(xs) + 1) * cell
    y0, y1 = min(ys) * cell, (max(ys) + 1) * cell
    mx, my = int((x1 - x0) * 0.05) + cell // 2, int((y1 - y0) * 0.05) + cell // 2
    w, h = im.size
    return max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my)


def make_sheet(tiles, max_width):
    """tiles: [(label, PIL image)] -> one grid image with the label drawn on each tile."""
    Image, _, ImageDraw = _pil()
    n = len(tiles)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = int(math.ceil(n / float(cols)))
    cw = max(200, int(max_width) // cols)
    cells = []
    for label, im in tiles:
        h = max(1, int(im.height * cw / float(im.width)))
        cells.append((label, im.resize((cw, h))))
    ch = max(c[1].height for c in cells)
    sheet = Image.new("RGB", (cw * cols, ch * rows), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    for i, (label, im) in enumerate(cells):
        x, y = (i % cols) * cw, (i // cols) * ch
        sheet.paste(im, (x, y))
        d.rectangle([x, y, x + 8 + 6 * len(label), y + 14], fill=(0, 0, 0))
        d.text((x + 4, y + 1), label, fill=(255, 255, 255))
    return sheet


# ---------------------------------------------------------------- tools
def viewport(W, p):
    """Apply a scene (persistently) and/or move the camera; save, list or delete named views."""
    spec = p.get("scene") or {}
    view = p.get("view")
    ents = validate(W, spec, [view] if view else [])
    vspec = resolve_view(W, view) if view else None
    apex = W.ensure_apex()
    cam = apex.display.getCamera()
    sess = Scene(W, spec, ents)
    sess.apply()
    out = {"applied": sess.applied, "warnings": sess.warnings}
    if vspec:
        out["camera"] = aim(W, cam, vspec)
        apex.display.render()
    if p.get("save_view"):
        out["saved_view"] = p["save_view"]
        out["camera"] = save_view(W, p["save_view"], cam)
    if p.get("delete_view"):
        name = p["delete_view"]
        path = _view_file(W, name) if NAME_OK.match(name) else None
        if not path or not os.path.isfile(path):
            raise KeyError("no saved view '%s'. Saved: %s" % (name, sorted(list_saved(W))))
        os.remove(path)
        out["deleted_view"] = name
    if p.get("list_views") or p.get("save_view") or p.get("delete_view"):
        out["saved_views"] = list_saved(W)
    if not out["applied"] and not vspec and not p.get("list_views") and not p.get("save_view") \
            and not p.get("delete_view"):
        out["camera"] = camera_state(cam)  # nothing to do: report where the camera is
    return out


def screenshot(W, p):
    """Apply a scene, capture each view, put the user's scene back."""
    apex = W.ensure_apex()
    spec = p.get("scene") or {}
    views = list(p.get("views") or [None])
    restore = bool(p.get("restore", True))
    crop = p.get("crop") or "model"
    layout = p.get("layout") or "sheet"
    if crop not in ("model", "none"):
        raise ValueError("crop must be 'model' or 'none'")
    if layout not in ("sheet", "separate"):
        raise ValueError("layout must be 'sheet' or 'separate'")
    max_w = int(p.get("max_width") or W.CFG.get("screenshot_max_width", 1400))
    margin = float(p.get("margin") or 0.85)
    if not 0.3 <= margin <= 1.0:
        raise ValueError("margin must be between 0.3 and 1.0 (1.0 = Apex's tight fit)")
    ents = validate(W, spec, views)
    vspecs = [resolve_view(W, v) for v in views]
    window = ensure_visible(W)
    Image = _pil()[0]
    cam = apex.display.getCamera()
    cam0 = camera_state(cam)
    sess = Scene(W, spec, ents)
    warnings = sess.warnings
    shots, not_restored = [], []
    try:
        sess.snapshot()
        sess.apply()
        ref = None
        if crop == "model" and Image is not None:
            cur = read_vis(W)  # with children: show() on a part cascades, so they must be put back too
            for part, _ in cur["parts"]:
                part.hide()
            try:
                ref = capture(W, "ref")[1]
            finally:
                write_vis(cur)
        for vs in vspecs:
            camera = aim(W, cam, vs, margin)
            ensure_visible(W)
            path, im = capture(W, "shot")
            box = None
            if im is not None and crop == "model":
                box = model_box(ref, im)
                if box is None:
                    warnings.append("view '%s': no model detected in the frame, full viewport returned" % vs["name"])
            if im is not None:
                if box:
                    im = im.crop(box)
                if im.width > max_w:
                    im = im.resize((max_w, max(1, int(im.height * max_w / float(im.width)))))
                im.save(path)
            shots.append({"name": vs["name"], "path": path, "size": list(im.size) if im is not None else None,
                          "cropped": bool(box), "camera": camera, "_img": im})
    finally:
        if restore:
            try:
                not_restored = sess.restore()
                set_camera(W, cam, cam0)
                apex.display.render()
            except Exception as exc:  # never hide the original error behind a restore failure
                not_restored = list(not_restored) + ["restore failed: %s" % exc]
    out = {"views": [{k: v for k, v in s.items() if k != "_img"} for s in shots], "applied": sess.applied,
           "restored": restore, "not_restored": not_restored, "window": window, "warnings": warnings}
    if layout == "sheet" and len(shots) > 1 and Image is not None and all(s["_img"] is not None for s in shots):
        sheet = make_sheet([(s["name"], s["_img"]) for s in shots], max_w)
        sheet_path = os.path.join(W.out_dir("screenshots"), "sheet_%s.png" % W.stamp())
        sheet.save(sheet_path)
        out["sheet"] = sheet_path
    return out


TOOLS = {"viewport": viewport, "screenshot": screenshot}
