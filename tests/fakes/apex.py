"""Tiny fake of the apex API surface used by the worker (no Apex needed).

`part.elements`, `part.nodes`, `mesh.elements`, `mesh.nodes` raise on access: the tools
must never walk element/node collections, and the end-to-end test proves it.
"""
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


EntityCollection = Coll


class Pt:
    def __init__(self, x=0, y=0, z=0): self.x, self.y, self.z = x, y, z


class BB:
    def __init__(self, lo=(0, 0, 0), hi=(10, 20, 30)): self.point1, self.point2 = Pt(*lo), Pt(*hi)


class _Forbidden:
    def __get__(self, obj, owner):
        raise AssertionError("tool walked an element/node collection - not allowed")


class Named:
    entityType = "EntityType.X"
    def __init__(self, name, path=None, id=1): self.name, self.pathName, self.id = name, path or name, id
    def getAxisAlignedBoundingBox(self): return BB()
    def highlight(self, colorRGB=None): pass


class Edge(Named):
    entityType = "EntityType.Edge"
    def __init__(self, L): super().__init__("e"); self.length = L


class Face(Named):
    entityType = "EntityType.Face"
    def __init__(self, A): super().__init__("f"); self.area = A


class Solid(Named):
    entityType = "EntityType.Solid"
    def getVolume(self): return 1234.5
    def getFaces(self): return Coll([Face(10.0), Face(0.001)])
    def getEdges(self): return Coll([Edge(5.0), Edge(0.02), Edge(40.0)])
    def getCells(self): return Coll([1])


class Surface(Named):
    entityType = "EntityType.Surface"
    isManifold = True
    def getArea(self): return 55.0
    def getFaces(self): return Coll([Face(55.0)])
    def getEdges(self): return Coll([Edge(10.0)] * 4)
    def getExteriorEdges(self): return Coll([Edge(10.0)] * 4)


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
    def __init__(self, name, n):
        super().__init__(name, "Model/PID53/" + name); self.n = n
    def getElementIDs(self): return {"ElementTopology.Quad4": list(range(1, self.n + 1)),
                                      "ElementTopology.Tri3": [self.n + 1]}
    def getNodeIDs(self): return list(range(1, self.n + 50))
    def getNodeCoordinates(self): return [0.0, 0.0, 0.0, 1.0, 2.0, 3.0]
    def getAssociatedGeometry(self): return Surface("skin_surf")
    def getElements(self, ids=None, indices=None):
        out = Coll()
        for rng in ids.split(","):
            a, b = rng.split("-")
            for i in range(int(a), int(b) + 1):
                el = Element("el", id=i); out.append(el)
        return out


class Part(Named):
    entityType = "EntityType.Part"
    elements = _Forbidden()
    nodes = _Forbidden()
    def __init__(self, name, path, meshes=(), solids=(), surfaces=()):
        super().__init__(name, path)
        self._m, self._so, self._su, self.vis = Coll(meshes), Coll(solids), Coll(surfaces), "Visible"
    def getMeshes(self): return self._m
    def getSolids(self): return self._so
    def getSurfaces(self): return self._su
    def getVisibility(self): return "Visibility." + self.vis
    def show(self): self.vis = "Visible"
    def hide(self): self.vis = "Hidden"
    def showOnly(self): self.vis = "Visible"
    def highlight(self, colorRGB=None): pass
    def update(self, **kw): self.kw = kw


P53 = Part("PID53", "Model/PID53", meshes=[Mesh("skin", 90000), Mesh("longeron", 500)], surfaces=[Surface("s1")])
P54 = Part("PID54", "Model/PID54", solids=[Solid("bracket_%d" % i, id=i) for i in range(9)],
           surfaces=[Surface("loose_%d" % i) for i in range(7)])


class Model(Part):
    entityType = "EntityType.Model"
    def getParts(self, recursive=False): return Coll([P53, P54])
    def getAssemblies(self, recursive=False): return Coll()


_M = Model("RATO", "Model")
def currentModel(): return _M
def getApplicationInfo(): return {"name": "MSC Apex", "release": "FAKE"}
def getScriptUnitSystemLabel(): return "mm-kg-s-N"
def getApplicationUnitSystemLabel(): return "mm-kg-s-N"
def getCurrentProjectFolderPath(): return "C:/fake"
def setScriptUnitSystem(unitSystemName=None): pass
def getEntities(pathNames=None): return Coll([e for e in (P53, P54, _M) if e.pathName in pathNames])
def get(target=None): return Coll([Element("el", id=1)])


class Scn:
    entityType = "EntityType.Scenario"; name = "Scn"; pathName = "Study/Scn"; solutionType = "S.Linear"
class Study:
    name = "Study"; pathName = "Study"; entityType = "EntityType.Study"
    def getScenarios(self, recursive=False): return Coll([Scn()])
    def getScenariosNastran(self): return Coll()
def getPrimaryStudy(): return Study()


ColorRGB = lambda r, g, b: (r, g, b)  # noqa: E731
session = types.SimpleNamespace(showAll=lambda: None, hideAll=lambda: None, clearHighlights=lambda: None,
                                DisplayRenderStyle=_Enum("Shaded", "Wireframe"),
                                displayConnectionMarkers=lambda display: display)


class Cam:
    focalPoint = Pt(1, 2, 3); position = Pt(); viewUp = Pt(0, 0, 1); distance = 5.0; parallelScale = 1.0
    def getDirectionOfProjection(self): return Pt(-1, -1, -1)
    def __getattr__(self, n): return lambda **kw: None


def _capture(path=None, imageNamePrefix="Image", imageCaptureRegion=None, imageFormat=None):
    from PIL import Image as I
    p = os.path.join(path, imageNamePrefix + ".png"); I.new("RGB", (2000, 1000), (40, 90, 160)).save(p); return p


display = types.SimpleNamespace(getCamera=lambda: Cam(), render=lambda: None, captureImage=_capture,
                                CaptureRegionType=_Enum("Viewport"), ImageType=_Enum("png", "jpg"))
construct = types.SimpleNamespace(Point3D=Pt)
Coordinate = Pt


class _Faults:
    numGeometryFaults = 2
    faultTypes = ["GeometryFaultType.SmallEdge", "GeometryFaultType.Sliver"]
    faults = [(Edge(0.02), Solid("bracket_0"), P54), (Face(0.001), Solid("bracket_3"), P54)]


class _Small:
    numFeaturesIdentified = 1
    smallFeatures = [{"type": "SmallEdge", "length": 0.02}]


geometry = types.SimpleNamespace(findGeometryFaults=lambda target=None: _Faults(),
                                 findFixSmallSurfaceAndCurveFeatures=lambda **kw: _Small())


def _getter(target=None):
    if target and "path" in target[0] and target[0]["path"] == "Model":
        return Coll([Named("Glue 1")])
    raise TypeError("bad target")


attribute = types.SimpleNamespace(getFasteners=_getter, getRigidLinks=_getter,
                                  getLayeredPanels=lambda target=None: Coll([Named("Bulkhead_layup")]))
catalog = types.SimpleNamespace(getMaterials=lambda target=None: Coll([Named("Al 2024-T3"), Named("CFRP_UD")]),
                                getAssignedMaterials=lambda: Coll([Named("CFRP_UD")]),
                                getPropertiesElement2Ds=lambda target=None: Coll([Named("PCOMP_skin")]),
                                getPropertiesElement3Ds=lambda target=None: Coll())
environment = types.SimpleNamespace(getLoads=lambda: Coll(), getConstraints=lambda: Coll([Named("Clamp")]))
