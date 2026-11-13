"""apex_describe without Apex: the worker side (introspect.describe) on a synthetic apex module tree
and the fake entities, and the text the server makes of it (describe_text).
Run from the venv:  python tests\\test_describe.py
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "worker"),
                os.path.join(HERE, "fakes")]

import apex as FAKE  # noqa: E402  (the fake, for entities)
import describe_text as DT  # noqa: E402
import introspect as IN  # noqa: E402
import serialize as S  # noqa: E402
import skills  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_describe_")


# ------------------------------------------------------------------ a synthetic apex
def make_apex():
    apex = types.ModuleType("apex")
    mesh = types.ModuleType("leaf.mesh")
    attribute = types.ModuleType("leaf.attribute")

    def getEntities(pathNames: "std::vector< std::string,std::allocator< std::string > > const &") \
            -> "ObjectSPtr< glef::EntityCollection >":
        return pathNames

    def createSolidMesh(*args, **kwargs):
        return None
    createSolidMesh.__module__ = "leaf.mesh"

    class SurfaceMeshElementShape:
        Mixed, Quadrilateral, Triangle, Undefined = 0, 1, 2, 3
        thisown = property(lambda self: True)
    SurfaceMeshElementShape.__module__ = "leaf.mesh"

    class ApplicationMethod:  # defined in attribute, imported into mesh as well
        Direct, Remote = 0, 1
    ApplicationMethod.__module__ = "leaf.attribute"

    class Entity:
        def hide(target) -> "void":
            return None

    class Part(Entity):
        name = property(lambda self: "P")
        elements = property(lambda self: (_ for _ in ()).throw(AssertionError("elements read")))

        def getMeshes(target) -> "ObjectSPtr< apex::mesh::MeshBodyCollection >":
            return []

        def update(self, *args, **kwargs) -> "void":
            return None
    for c in (Entity, Part):
        c.__module__ = "glef"

    mesh.createSolidMesh, mesh.SurfaceMeshElementShape, mesh.ApplicationMethod = (
        createSolidMesh, SurfaceMeshElementShape, ApplicationMethod)
    attribute.ApplicationMethod = ApplicationMethod
    apex.getEntities, apex.Part, apex.mesh, apex.attribute = getEntities, Part, mesh, attribute
    apex.os = os  # Apex's namespace re-exports os, sys, site: they are not apex modules
    return apex


APEX = make_apex()


def d(target, **kw):
    return IN.describe(APEX, target, **kw)


def test_module_lists_its_own_names_and_counts_reexports_apart():
    r = d("apex.mesh")
    assert r["kind"] == "module" and r["runtime_name"] == "leaf.mesh", r
    assert r["functions"]["names"] == ["createSolidMesh"] and r["enums"]["names"] == ["SurfaceMeshElementShape"], r
    assert r["reexported"] == {"apex.attribute": 1}, r
    assert d("apex")["modules"]["names"] == ["attribute", "mesh"]  # not the os module Apex re-exports


def test_function_signatures_are_simplified_and_star_is_reported():
    r = d("apex.getEntities")
    assert r["kind"] == "function" and r["module"] == "apex", r
    assert r["signature"] == {"params": [{"name": "pathNames", "type": "[str]"}], "returns": "EntityCollection"}, r
    assert d("apex.mesh.createSolidMesh")["signature"] == {"star": True}


def test_enum_class_method_and_property():
    assert d("apex.mesh.SurfaceMeshElementShape")["members"] == {"Mixed": 0, "Quadrilateral": 1, "Triangle": 2, "Undefined": 3}
    r = d("apex.Part")
    assert r["kind"] == "class" and r["bases"] == ["Entity"], r
    assert r["properties"]["names"] == ["elements", "name"] and r["methods"]["names"] == ["getMeshes", "hide", "update"], r
    r = d("apex.Part.getMeshes")
    assert r["kind"] == "method" and r["signature"] == {"params": [], "returns": "MeshBodyCollection"}, r
    assert r["owner"] == "Part" and r["owner_bases"] == ["Entity"], r
    assert d("apex.Part.name") == {"target": "apex.Part.name", "kind": "property", "owner": "Part"}
    assert d("apex.Part", flt="mesh")["methods"]["names"] == ["getMeshes"]


def test_entities_and_variables_never_read_element_collections():
    part = FAKE.PARTS[0]  # the fake Part raises if .elements / .nodes is read
    r = IN.describe(FAKE, "Model/PID53", get_entity=lambda p: next(x for x in FAKE.PARTS if x.pathName == p), ref=S.ref)
    assert r["kind"] == "object" and r["ref"]["path"] == "Model/PID53", r
    assert {"elements", "nodes"} <= set(r["not_read"]) and "elements" not in r["properties"], r
    assert r["properties"]["name"] == "PID53" and r["properties"]["color"] == {"type": "list", "count": 3, "values": [255, 255, 0]}, r
    r = IN.describe(FAKE, "$p", ns={"p": part}, ref=S.ref, values=False)
    assert "elements" in r["properties"]["names"] and "not_read" not in r, r
    for bad in ("$p.elements", "$p.nodes"):
        try:
            IN.describe(FAKE, bad, ns={"p": part})
            raise AssertionError("read " + bad)
        except ValueError as exc:
            assert "bulk getters" in str(exc)
    try:
        IN.describe(FAKE, "$q", ns={"p": part})
        raise AssertionError("unknown variable accepted")
    except KeyError as exc:
        assert "it holds: p" in str(exc)


def test_misses_are_quiet_and_suggest_close_names():
    for target, text in (("apex.getEntitie", "apex has no 'getEntitie' in this Apex. Close: getEntities"),
                         ("apex.mesh.SurfaceMeshElementShape.Quad", "apex.mesh.SurfaceMeshElementShape has no 'Quad' in this Apex. Close: Quadrilateral")):
        try:
            d(target)
            raise AssertionError("resolved " + target)
        except IN.NotFound as exc:
            assert exc.quiet and str(exc).startswith(text), str(exc)
    try:
        IN.describe(FAKE, "Model/Nope", get_entity=lambda p: (_ for _ in ()).throw(KeyError("unknown path")))
        raise AssertionError("resolved a missing entity")
    except IN.NotFound as exc:
        assert "no entity 'Model/Nope' (unknown path)" in str(exc), str(exc)


def test_limit_caps_values_read_and_names_the_rest():
    class Obj:
        pass
    o = Obj()
    for n in ("a", "b", "c", "d", "elements"):
        setattr(o, n, 1)
    r = IN.describe(FAKE, "$o", ns={"o": o}, limit=2)
    assert list(r["properties"]) == ["a", "b"] and r["not_shown"] == ["c", "d"], r
    assert r["not_read"] == ["elements"], r  # skipped names do not use up the limit
    assert "not shown (limit reached" in DT.format_describe(r, None, {})


def test_values_are_summarised():
    class Pt:
        x, y, z = 1.0, 2.0, 3.0

    class BB:
        point1, point2 = Pt(), Pt()

    class RGB:
        r = property(lambda self: 1)
        g = property(lambda self: 2)
    assert IN.summarize(BB()) == {"point1": [1.0, 2.0, 3.0], "point2": [1.0, 2.0, 3.0]}
    assert IN.summarize(RGB()) == {"type": "RGB", "g": 2, "r": 1}
    assert IN.summarize(FAKE.Coll([1, 2])) == {"type": "Coll", "count": 2, "values": [1, 2]}
    assert IN.summarize("x" * 300).endswith("...")
    assert IN.ctype("std::map< std::string,ObjectSPtr< glef::Entity > >") == "{str:Entity}"
    assert IN.ctype("std::vector< int,std::allocator< int > > const &") == "[int]"


def test_big_property_names():
    for n in ("elements", "nodes", "displayableEntities", "elementIDs", "nodeIds", "nodeCoordinates"):
        assert IN.BIG_PROPERTY.search(n), n
    for n in ("solids", "surfaces", "name", "meshes", "elementOrder", "ids_count"):
        assert not IN.BIG_PROPERTY.search(n), n


# ------------------------------------------------------------------ text, with a small reference
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def skill():
    api = os.path.join(TMP, "skill", "api")
    write(os.path.join(api, "modules", "apex.mesh.md"),
          "## apex.mesh\n\n### `apex.mesh.createSolidMesh(name: str, target: apex.EntityCollection) -> None`\nMesh.\n")
    write(os.path.join(api, "modules", "apex.md"), "## apex\n\n### `apex.getEntities(pathNames: [str]) -> apex.EntityCollection`\nGet.\n")
    write(os.path.join(api, "enums.md"), "- `apex.mesh.SurfaceMeshElementShape`: `Mixed`, `Quadrilateral`, `Triangle`\n"
                                         "- `apex.attribute.ApplicationMethod`: `Direct`, `Remote`\n")
    write(os.path.join(api, "classes.md"), "- `apex.Part` extends Entity — A part.\n- `apex.Entity` — Base.\n")
    write(os.path.join(api, "classes", "apex.md"),
          "## `apex.Entity`\nBase.\n\nMethods:\n\n- `hide() -> None` — Hide this Entity.\n\n"
          "## `apex.Part`  (extends `Entity`)\nA part.\n\nMethods:\n\n- `getMeshes() -> MeshBodyCollection` — Meshes.\n")
    write(os.path.join(api, "methods-index.md"), "- `apex.Entity.hide` — Hide this Entity. (inherited by 404 classes)\n"
                                                  "- `apex.Part.getMeshes` — Meshes.\n")
    write(os.path.join(api, "functions-index.md"), "")
    return skills.ApexScriptingSkill(os.path.dirname(api))


def test_text_marks_undocumented_and_elsewhere_and_merges_signatures():
    sk = skill()
    mesh = dict(d("apex.mesh"), enums={"names": ["ApplicationMethod", "Nope", "SurfaceMeshElementShape"]})
    t = DT.format_describe(mesh, sk)
    assert "ApplicationMethod^" in t and "Nope*" in t and "SurfaceMeshElementShape\n" in t and "createSolidMesh" in t, t
    assert "^ = documented under another module" in t and "1 from apex.attribute" in t, t
    t = DT.format_describe(d("apex.mesh.createSolidMesh"), sk)
    assert "runtime:   apex.mesh.createSolidMesh(*args, **kwargs)" in t, t
    assert "reference: apex.mesh.createSolidMesh(name: str, target: apex.EntityCollection) -> None" in t, t
    t = DT.format_describe(d("apex.getEntities"), sk)
    assert "runtime:   apex.getEntities(pathNames: [str]) -> EntityCollection" in t, t
    t = DT.format_describe(d("apex.mesh.SurfaceMeshElementShape"), sk)
    assert "Undefined=3*" in t and "Mixed=0," in t, t
    t = DT.format_describe(d("apex.Part"), sk)
    assert "A part." in t and "hide," in t and "update*" in t and "getMeshes," in t, t  # hide: inherited, via the MRO
    t = DT.format_describe(d("apex.Part.hide"), sk)
    assert "reference: Part.hide() -> None — Hide this Entity. (inherited from apex.Entity)" in t, t
    t = DT.format_describe(IN.describe(FAKE, "$p", ns={"p": FAKE.PARTS[0]}, ref=S.ref), sk)
    assert "name = 'PID53'" in t and "not read" in t and "elements" in t and "update(...)" in t, t
    t = DT.format_describe({"target": "apex.ConstraintAxis", "kind": "enum", "members": {"X": 0}},
                           sk, {"moved": {"apex.ConstraintAxis": "apex.session.ConstraintAxis"}})
    assert "in this Apex it is apex.session.ConstraintAxis" in t, t


def test_method_signature_follows_the_runtime_bases():
    sk = skill()
    assert sk.method_signature("Part", "hide") == []  # the index does not name Part among the 404 heirs
    assert sk.method_signature("Part", "hide", ["Entity"]) == \
        ["Part.hide() -> None — Hide this Entity. (inherited from apex.Entity)"]
    assert sk.documented_methods("Part") == {"getMeshes"}
    assert sk.documented_methods("Part", ["Entity"]) == {"getMeshes", "hide"}


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
