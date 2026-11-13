"""Tiny fake of the apex API surface used by the worker (no Apex needed).

`part.elements`, `part.nodes`, `mesh.elements`, `mesh.nodes` raise on access: the tools
must never walk element/node collections, and the end-to-end test proves it. Functions the
builders call are keyword-only, like the real API.
"""
import math
import os
import types


class _Enum:
    def __init__(self, *names):
        for n in names:
            setattr(self, n, "E." + n)


class Coll(list):
    def len(self): return len(self)
    def append(self, entity=None): list.append(self, entity)
    def extend(self, collection=None): list.extend(self, collection)
    def appendList(self, lst): list.extend(self, lst)


EntityCollection = ILocationCollection = Coll
geometry_EdgeCollection = Coll


class Pt:
    def __init__(self, x=0, y=0, z=0): self.x, self.y, self.z = x, y, z


class BB:
    def __init__(self, lo=(0, 0, 0), hi=(10, 20, 30)): self.point1, self.point2 = Pt(*lo), Pt(*hi)


class _Forbidden:
    def __get__(self, obj, owner):
        raise AssertionError("tool walked an element/node collection - not allowed")


_IDS = {"n": 1000}


def _next_id():
    _IDS["n"] += 1
    return _IDS["n"]


class Named:
    entityType = "EntityType.X"
    def __init__(self, name, path=None, id=1): self.name, self.pathName, self.id = name, path or name, id
    def getAxisAlignedBoundingBox(self): return BB()
    def highlight(self, colorRGB=None): pass
    def getPathName(self): return self.pathName


class Node(Named):
    entityType = "EntityType.Node"
    def __init__(self, id, xyz=(0, 0, 0)): super().__init__("n%d" % id, "node%d" % id, id); self.coordinates = Pt(*xyz)


class Edge(Named):
    entityType = "EntityType.Edge"
    def __init__(self, L): super().__init__("e"); self.length = L


class Face(Named):
    """Plane (normal +z) or cylinder (axis z through the origin, radius r)."""
    entityType = "EntityType.Face"
    def __init__(self, A, id=1, centroid=(0, 0, 0), cyl_r=None, node_ids=(1, 2, 3)):
        super().__init__("f", "f%d" % id, id)
        self.area, self.centroid, self.cyl_r, self.node_ids = A, Pt(*centroid), cyl_r, node_ids
    def getParametricRange(self): return {"uStart": 0.0, "uEnd": 1.0, "vStart": 0.0, "vEnd": 1.0}
    def evaluateNormal(self, *, u, v):
        if self.cyl_r is None:
            return Pt(0, 0, 1)
        a = 2 * math.pi * u
        return Pt(math.cos(a), math.sin(a), 0)
    def evaluateFaceParametricCoordinate(self, *, u, v):
        a = 2 * math.pi * u
        return Pt(self.cyl_r * math.cos(a), self.cyl_r * math.sin(a), v) if self.cyl_r else Pt(u, v, 0)
    def getCylindricalAxis(self): return Pt(0, 0, 1)
    def getCylindricalAxisMidPoint(self): return Pt(0, 0, 0.5)
    def getNodes(self): return Coll([Node(i) for i in self.node_ids])


class Solid(Named):
    entityType = "EntityType.Solid"
    vis, parent, writes = "Visible", None, []  # writes: every explicit show()/hide() call, for the tests
    def getVisibility(self): return "Visibility." + self.vis
    def show(self): self._set("Visible")
    def hide(self): self._set("Hidden")
    def _set(self, v):
        Solid.writes.append((self.pathName, v))
        self.vis = v
        if self.parent is not None and self.parent._cascading is False:
            self.parent.vis = "NotSet"  # like Apex: an explicit change on a child makes the container NotSet (-1)
    def getVolume(self): return 1234.5
    def getFaces(self): return Coll([Face(10.0, id=101, centroid=(0, 0, 0)),
                                     Face(0.001, id=102, centroid=(0, 0, 0.5), cyl_r=5.0, node_ids=(2, 3, 4))])
    def getEdges(self): return Coll([Edge(5.0), Edge(0.02), Edge(40.0)])
    def getCells(self): return Coll([1])


class Surface(Named):
    entityType = "EntityType.Surface"
    isManifold = True
    def getArea(self): return 55.0
    def getFaces(self): return Coll([Face(55.0, id=201, centroid=(100, 0, 0))])
    def getEdges(self): return Coll([Edge(10.0)] * 4)
    def getExteriorEdges(self): return Coll([Edge(10.0)] * 4)


class Curve(Named):
    entityType = "EntityType.Curve"
    def __init__(self, pts): super().__init__("Curve %d" % _next_id()); self.pts = pts
    def getEdges(self): return Coll([Edge(1.0)])
    def update(self, *, name=None): self.name = name


class Prop(Named):
    pass


class Element(Named):
    entityType = "EntityType.Element"
    topology = "ElementTopology.Quad4"
    elementProperty2D = Prop("PCOMP_skin")
    elementProperty3D = None
    material = Prop("CFRP_UD")
    thickness = 1.5


class Mesh(Named):
    entityType = "EntityType.SurfaceMesh"
    elements = _Forbidden()
    nodes = _Forbidden()
    def __init__(self, name, n, path="Model/PID53", geo=None, xyz=None, first_id=1):
        super().__init__(name, path + "/" + name); self.n, self.geo = n, geo
        self.xyz = xyz or [(i * 0.01, 0.0, 0.0) for i in range(n + 49)]
        self.ids = list(range(first_id, first_id + len(self.xyz))) if xyz is None else [_next_id() for _ in self.xyz]
    def getElementIDs(self): return {"ElementTopology.Quad4": list(range(1, self.n + 1)),
                                      "ElementTopology.Tri3": [self.n + 1]}
    def getNodeIDs(self): return list(self.ids)
    def getNodeCoordinates(self): return [c for p in self.xyz for c in p]
    def getAssociatedGeometry(self): return self.geo if self.geo is not None else Surface("skin_surf")
    def getElements(self, ids=None, indices=None):
        out = Coll()
        for rng in ids.split(","):
            a, b = rng.split("-")
            for i in range(int(a), int(b) + 1):
                el = Element("el", id=i); out.append(el)
        return out
    def getNodes(self, *, ids):
        want = set()
        for rng in ids.split(","):
            a, b = rng.split("-")
            want.update(range(int(a), int(b) + 1))
        return Coll([Node(i, p) for i, p in zip(self.ids, self.xyz) if i in want])
    def getNode(self, *, id):
        return next(Node(i, p) for i, p in zip(self.ids, self.xyz) if i == id)


_RENDER = {"E." + n: i for i, n in enumerate(
    ("Wireframe", "HiddenLines", "Filled", "FilledWithEdges", "Shaded", "ShadedWithEdges"))}


class Part(Named):
    entityType = "EntityType.Part"
    elements = _Forbidden()
    nodes = _Forbidden()
    def __init__(self, name, path, meshes=(), solids=(), surfaces=()):
        super().__init__(name, path)
        self._m, self._so, self._su, self.vis = Coll(meshes), Coll(solids), Coll(surfaces), "Visible"
        self.color, self.render, self.transp = [255, 255, 0], "-5", 0  # "-5": render style not set
        self._cascading, self.asm = False, None
        for so in self._so: so.parent = self
    def getMeshes(self): return Coll(self._m)
    def getSolids(self): return self._so
    def getSurfaces(self): return self._su
    def getVisibility(self): return "Visibility." + self.vis
    def _cascade(self, v):  # like Apex: a container cascades to its children, and stays Visible/Hidden
        self.vis, self._cascading = v, True
        for so in self._so: so._set(v)
        self._cascading = False
        if self.asm is not None and not self.asm._cascading:
            self.asm.vis = "NotSet"  # changing one part alone makes its assembly NotSet, and it stays so
    def show(self): self._cascade("Visible")
    def hide(self): self._cascade("Hidden")
    def showOnly(self): self.vis = "Visible"
    def highlight(self, colorRGB=None): pass
    def getColor(self): return list(self.color)
    def getDisplayRenderStyle(self): return self.render
    def getDisplayTransparency(self): return self.transp
    def unsetRender(self): self.render = "-5"
    def update(self, **kw):
        self.kw = kw
        if "color" in kw: self.color = list(kw["color"])
        if "renderStyle" in kw: self.render = str(_RENDER.get(kw["renderStyle"], "-5"))
        if "transparencyLevel" in kw: self.transp = kw["transparencyLevel"]


P53 = Part("PID53", "Model/PID53", meshes=[Mesh("skin", 90000), Mesh("longeron", 500, first_id=200000)], surfaces=[Surface("s1")])
P54 = Part("PID54", "Model/PID54", solids=[Solid("bracket_%d" % i, "Model/PID54/bracket_%d" % i, id=i) for i in range(9)],
           surfaces=[Surface("loose_%d" % i, "Model/PID54/loose_%d" % i) for i in range(7)])
PARTS = [P53, P54]


ASSEMBLIES = []  # empty unless a test adds one


class Assembly(Named):
    entityType = "EntityType.Assembly"
    def __init__(self, name, parts):
        super().__init__(name, "Model/" + name)
        self.parts, self.vis, self._cascading = list(parts), "Visible", False
        for p in self.parts: p.asm = self
    def getVisibility(self): return "Visibility." + self.vis
    def _cascade(self, v):
        self.vis, self._cascading = v, True
        for p in self.parts: p._cascade(v)
        self._cascading = False
    def show(self): self._cascade("Visible")
    def hide(self): self._cascade("Hidden")


class Model(Part):
    entityType = "EntityType.Model"
    current = None
    def getParts(self, recursive=False): return Coll(PARTS)
    def getAssemblies(self, recursive=False): return Coll(ASSEMBLIES)
    def createPart(self, *, name):
        p = Part(name, self.pathName + "/" + name); PARTS.append(p); return p
    def setCurrentPart(self, *, pathName):
        self.current = next(p for p in PARTS if p.pathName == pathName); return self.current
    def exportFEModel(self, *, filename, unitSystem):
        _check_units(unitSystem); _write_bdf(filename, None)


_M = Model("RATO", "Model")
def currentModel(): return _M
def getApplicationInfo(): return {"name": "MSC Apex", "release": "FAKE"}
def getScriptUnitSystemLabel(): return "mm-kg-s-N"
def getApplicationUnitSystemLabel(): return "mm-kg-s-N"
def getCurrentProjectFolderPath(): return "C:/fake"
def setScriptUnitSystem(unitSystemName=None): pass
def getEntities(pathNames=None):
    return Coll([e for e in PARTS + [_M] + [so for pt in PARTS for so in pt._so] if e.pathName in pathNames])
def get(target=None): return Coll([Element("el", id=1)])
def getPart(*, pathName): return next((p for p in PARTS if p.pathName == pathName), None)
def getAssembly(*, pathName): return _M if pathName == _M.pathName else None


# ------------------------------------------------------------- world state for builders
W = {"materials": {}, "props3d": {}, "shapes": {}, "interactions": [], "ties": [], "loads": [],
     "constraints": [], "scenarios": [], "deleted": 0}


class Material(Named):
    def __init__(self, name):
        super().__init__(name, "catalog/" + name, _next_id()); self.props = {}
    def getMaterialModel(self, *, materialModel):
        mat = self
        return types.SimpleNamespace(update=lambda *, materialProperties: mat.props.update(materialProperties))


class Prop3D(Named):
    def __init__(self, name):
        super().__init__(name, "catalog/" + name, _next_id()); self.mid = None
    def getProperties3DModel(self, *, properties3DModel):
        prop = self
        return types.SimpleNamespace(update=lambda *, elementProperties3D: setattr(prop, "mid", elementProperties3D["MID"]))


def _create_material(*, name, primaryMaterialModel):
    W["materials"][name] = Material(name); return W["materials"][name]


def _create_prop3d(*, name, primaryProperties3D):
    W["props3d"][name] = Prop3D(name); return W["props3d"][name]


catalog = types.SimpleNamespace(
    getMaterials=lambda target=None: Coll([Named("Al 2024-T3"), Named("CFRP_UD")] + list(W["materials"].values())),
    getMaterialDictionary=lambda: dict(W["materials"]),
    getAssignedMaterials=lambda: Coll([Named("CFRP_UD")]),
    getPropertiesElement2Ds=lambda target=None: Coll([Named("PCOMP_skin")]),
    getPropertiesElement3Ds=lambda target=None: Coll(W["props3d"].values()),
    getPropertiesElement3DDictionary=lambda: dict(W["props3d"]),
    getBeamShapeDictionary=lambda: dict(W["shapes"]),
    createMaterial=_create_material, createPropertiesElement3D=_create_prop3d)


def deleteEntities(*, target):
    for e in target:
        W["deleted"] += 1
        for key in ("interactions", "ties", "loads", "constraints", "scenarios"):
            if e in W[key]:
                W[key].remove(e)
        for p in PARTS:
            if e in p._m:
                p._m.remove(e)


class Scn:
    entityType = "EntityType.Scenario"; name = "Scn"; pathName = "Study/Scn"; solutionType = "S.Linear"


class Command:
    def __init__(self): self.value, self.active = None, False
    def setValue(self, v): self.value = v
    def getValue(self): return self.value


class Subcase:
    def __init__(self, i): self.name, self.i, self.cmds = "Subcase %d" % i, i, {}
    def getCommand(self, *, commandName): return self.cmds.setdefault(commandName, Command())
    def setCommand(self, *, commandName, activate): self.cmds.setdefault(commandName, Command()).active = activate
    def setName(self, n): self.name = n
    def setLabel(self, n): pass
    def setSubtitle(self, n): pass
    def getSubcaseID(self): return self.i


class ScnNastran(Named):
    entityType = "EntityType.ScenarioNastran"
    def __init__(self, name, n):
        super().__init__(name, "Study/" + name, _next_id()); self.subs = [Subcase(i + 1) for i in range(n)]
    def getSubcases(self): return Coll(self.subs)
    def associateModelRep(self, *, modelRep): self.rep = modelRep
    def exportFEModel(self, *, filename, unitSystem, resultOutputType=None):
        _check_units(unitSystem); _write_bdf(filename, self)


def _check_units(label):
    if label.startswith("mm-kg"):  # both mm-kg labels are refused by Apex 2024.1
        raise RuntimeError("The specified units system is not supported")


def _write_bdf(filename, scn):
    lines = ["SOL 101", "CEND"]
    for s in (scn.subs if scn else []):
        lines += ["SUBCASE %d" % s.i] + ["  %s = %s" % (k, c.value) for k, c in s.cmds.items() if c.active]
    lines += ["BEGIN BULK", "GRID    1               0.      0.      0.", "GRID    2               1.      0.      0.",
              "MAT1    1       70000.          .3      2.7-9", "PSOLID  1       1",
              "BCONECT 1       1       1       2       1", "FORCE   5       1               1.      0.      0.      1.",
              "SPC1    7       123456  1", "RBE2    9       2       123456  1", "ENDDATA"]
    with open(filename, "w") as fh:
        fh.write("\n".join(lines) + "\n")


class Study:
    name = "Study"; pathName = "Study"; entityType = "EntityType.Study"
    def getScenarios(self, recursive=False): return Coll([Scn()])
    def getScenariosNastran(self): return Coll(W["scenarios"]) if W["scenarios"] else None  # Apex: None when empty
    def createScenarioNastran(self, *, scenarioNastranType, numberOfSubcases, subcaseType, name):
        s = ScnNastran(name, numberOfSubcases); W["scenarios"].append(s); return s
def getPrimaryStudy(): return Study()


studies = types.SimpleNamespace(NastranSolutionType=_Enum("Statics_101"), NastranAnalysisType=_Enum("Statics"))
ColorRGB = lambda r, g, b: (r, g, b)  # noqa: E731
AutoManual = _Enum("Auto", "Manual")
CALLS = []  # display/session calls the tests assert on


def _show_all():
    for p in PARTS: p.show()


def _hide_all():
    CALLS.append("session.hideAll")
    for p in PARTS: p.hide()


session = types.SimpleNamespace(showAll=_show_all, hideAll=_hide_all, clearHighlights=lambda: CALLS.append("clearHighlights"),
                                DisplayRenderStyle=_Enum("Wireframe", "HiddenLines", "Filled", "FilledWithEdges", "Shaded",
                                                         "ShadedWithEdges"),
                                displayConnectionMarkers=lambda display: CALLS.append(("connection", display)) or display,
                                displayLoadsAndBCMarkers=lambda display: CALLS.append(("loads_bc", display)) or display,
                                displayMeshTopologyEdges=lambda displayEdges: CALLS.append(("mesh_edges", displayEdges)) or displayEdges)

_SCALE = {"iso1": 120.0, "iso2": 121.0, "iso3": 122.0, "iso4": 123.0, "x": 100.0, "y": 150.0, "z": 200.0}


class Cam:
    """A camera that remembers what it was told, like Apex's (one object per view)."""
    def __init__(self):
        self.pos, self.foc, self.up, self.scale, self.calls = Pt(10, 20, 30), Pt(1, 2, 3), Pt(0, 0, 1), 100.0, []
    def _copy(self, p): return Pt(p.x, p.y, p.z)
    def getPosition(self): return self._copy(self.pos)
    def getFocalPoint(self): return self._copy(self.foc)
    def getViewUp(self): return self._copy(self.up)
    def getParallelScale(self): return self.scale
    def getDistance(self): return 10000.0
    def getDirectionOfProjection(self): return Pt(self.pos.x - self.foc.x, self.pos.y - self.foc.y, self.pos.z - self.foc.z)
    def setPosition(self, *, position): self.pos = self._copy(position)
    def setFocalPoint(self, *, focalPoint): self.foc = self._copy(focalPoint)
    def setViewUp(self, *, viewUp): self.up = self._copy(viewUp)
    def setParallelScale(self, *, parallelScale): self.scale = parallelScale
    def _preset(self, name, reverse=False, viewUp=None, reset=True):
        self.calls.append(("fit", name, reverse, viewUp))
        self.scale, self.foc = _SCALE[name], Pt(0, 0, 0)
        d = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}.get(name, (1, 1, 1))
        self.pos = Pt(*[(-1 if reverse else 1) * c for c in d])
    def viewFromX(self, reverse=False, viewUp=None, reset=True): self._preset("x", reverse, viewUp, reset)
    def viewFromY(self, reverse=False, viewUp=None, reset=True): self._preset("y", reverse, viewUp, reset)
    def viewFromZ(self, reverse=False, viewUp=None, reset=True): self._preset("z", reverse, viewUp, reset)
    def viewIsometric_1(self, reverse=False, viewUp=None, reset=True): self._preset("iso1", reverse, viewUp, reset)
    def viewIsometric_2(self, reverse=False, viewUp=None, reset=True): self._preset("iso2", reverse, viewUp, reset)
    def viewIsometric_3(self, reverse=False, viewUp=None, reset=True): self._preset("iso3", reverse, viewUp, reset)
    def viewIsometric_4(self, reverse=False, viewUp=None, reset=True): self._preset("iso4", reverse, viewUp, reset)
    def rotate(self, *, azimuth_angle=0.0, elevation_angle=0.0): self.calls.append(("rotate", azimuth_angle, elevation_angle))
    def roll(self, *, angle): self.calls.append(("roll", angle))
    def zoom(self, *, factor): self.calls.append(("zoom", factor)); self.scale /= factor
    def pan(self, *, horizontal_distance=0.0, vertical_distance=0.0): self.calls.append(("pan", horizontal_distance, vertical_distance))


_CAM = Cam()
CAP = {"empty": 0}  # the next N captures write a 0-byte file, like Apex with its UI hidden


def _capture(path=None, imageNamePrefix="Image", imageCaptureRegion=None, imageFormat=None):
    """Panel (always), scale bar (always) and a 'model' block that exists only while a part is visible."""
    from PIL import Image as I, ImageDraw as D
    p = os.path.join(path, imageNamePrefix + "_1.png")
    if CAP["empty"] > 0:
        CAP["empty"] -= 1
        open(p, "wb").close()
        return p
    im = I.new("RGB", (800, 400), (40, 90, 160))
    d = D.Draw(im)
    d.rectangle([5, 5, 150, 70], fill=(25, 25, 25))
    d.rectangle([650, 360, 790, 392], fill=(200, 200, 200))
    shown = [pt for pt in PARTS if pt.vis != "Hidden"]
    if shown:
        w = min(400, int(_CAM.scale * 2))
        d.rectangle([300, 120, 300 + w, 300], fill=tuple(shown[0].color))
    im.save(p)
    return p


TEXTS = []


def _display_text(text=None, textLocation=None, graphicsFontColor=None, graphicsFontSize=12, **kw):
    t = types.SimpleNamespace(text=text)
    TEXTS.append(t)
    return t


display = types.SimpleNamespace(getCamera=lambda: _CAM, render=lambda: None, captureImage=_capture,
                                CaptureRegionType=_Enum("Viewport"), ImageType=_Enum("png", "jpg"),
                                displayText=_display_text, clearText=lambda graphicsText: TEXTS.remove(graphicsText),
                                clearAllGraphicsText=lambda: TEXTS.clear(),
                                createCutView=lambda **kw: CALLS.append(("createCutView", kw)),
                                displayCutViews=lambda display=True, **kw: CALLS.append(("displayCutViews", display)))
construct = types.SimpleNamespace(Point3D=Pt, Vector3D=Pt)
Coordinate = Pt


class _Faults:
    numGeometryFaults = 2
    faultTypes = ["GeometryFaultType.SmallEdge", "GeometryFaultType.Sliver"]
    faults = [(Edge(0.02), Solid("bracket_0"), P54), (Face(0.001), Solid("bracket_3"), P54)]


class _Small:
    numFeaturesIdentified = 1
    smallFeatures = [{"type": "SmallEdge", "length": 0.02}]


def _create_curve(*, target, behavior):
    c = Curve([(p.x, p.y, p.z) for p in target]); return c


geometry = types.SimpleNamespace(findGeometryFaults=lambda target=None: _Faults(),
                                 findFixSmallSurfaceAndCurveFeatures=lambda **kw: _Small(),
                                 createCurve=_create_curve, CurveBehavior=_Enum("Polyline", "Spline"),
                                 EdgeCollection=Coll)


def _create_solid_mesh(*, name, target, meshSize, elementOrder):
    for i, s in enumerate(target):  # Apex: 'name', then 'name 1', 'name 2', ...
        part = next(p for p in PARTS if s in p._so)
        m = Mesh(name if i == 0 else "%s %d" % (name, i), 100, part.pathName, geo=s)
        m.id = _next_id()
        part._m.append(m)


def _create_curve_mesh(*, name, target, meshSize, elementOrder):
    part = _M.current
    for c in target:
        part._m.append(Mesh(name, len(c.pts) - 1, part.pathName, geo=c, xyz=c.pts))


def _create_node(*, coordinates, pathName):
    return Node(_next_id(), (coordinates.x, coordinates.y, coordinates.z))


mesh = types.SimpleNamespace(ElementOrder=_Enum("Linear", "Quadratic"), createSolidMesh=_create_solid_mesh,
                             createCurveMesh=_create_curve_mesh, createNodeByLocation=_create_node)


def _getter(target=None):
    if target and "path" in target[0] and target[0]["path"] == "Model":
        return Coll([Named("Glue 1")])
    raise TypeError("bad target")


class Tie(Named):
    def __init__(self, ref, n): super().__init__("Node_Tie", "ties/t", _next_id()); self.ref, self.n = ref, n
    def getReferencePoint(self): return self.ref
    def getAttachmentDOFAndWeights(self): return {str(i): {"123456": 1.0} for i in range(self.n)}


class Interaction(Named):
    def createInteractionRep(self, *, name, interactionType, interactionPropertyGeometric): self.rep = name


def _model_list(key):
    def get(target=None):
        if target and target[0].get("path") == "Model":
            return Coll(W[key])
        raise TypeError("bad target")
    return get


def _create_tie(*, referencePoint, dependentDoF, attachmentRegions, distributionType):
    t = Tie(referencePoint, len(attachmentRegions)); W["ties"].append(t); return t


def _create_pair(*, name, targetSide1, targetSide2):
    ia = Interaction(name, "interactions/" + name, _next_id()); W["interactions"].append(ia); return ia


def _create_span(*, name, beamTarget, shapeEndA, material):
    return Coll([Named(name)])


def _create_shape(*, name, radius):
    W["shapes"][name] = Named(name); return W["shapes"][name]


attribute = types.SimpleNamespace(
    getFasteners=_getter, getRigidLinks=_getter, getInteractions=_model_list("interactions"),
    getNodeTies=_model_list("ties"), getLayeredPanels=lambda target=None: Coll([Named("Bulkhead_layup")]),
    assignPropertiesElement3D=lambda *, target, property: None,
    createInteractionPropertyGeometric=lambda *, contactToleranceCalculationMethod, contactTolerance, name=None: Named(name or "geo"),
    createInteractionManualPair=_create_pair, createNodeTie=_create_tie, createBeamSpanFree=_create_span,
    createBeamShapeSolidRound=_create_shape,
    InteractionType=_Enum("Glue", "Contact"), DistributionType=_Enum("Rigid", "Compliant"),
    ConstraintType=_Enum("Clamped", "General", "Axial", "Symmetry", "Spherical"),
    ApplicationMethod=_Enum("Direct", "Remote"), NastranResultOutputType=_Enum("Hdf5", "Op2"))


def _create_entity(key, prefix):
    def create(*, name, target, applicationMethod, id=None, **kw):
        e = Named(name, prefix + name, id if id is not None else _next_id()); e.kw = kw; W[key].append(e); return e
    return create


environment = types.SimpleNamespace(
    getLoads=lambda: Coll(W["loads"]) if W["loads"] else Coll(),
    getConstraints=lambda: Coll([Named("Clamp")] + W["constraints"]),
    createConstraintDisplacement=_create_entity("constraints", "constraints/"),
    createLoadForceComponent=_create_entity("loads", "loads/"),
    createLoadMomentComponent=_create_entity("loads", "loads/"))
