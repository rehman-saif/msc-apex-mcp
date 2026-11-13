"""Scene/viewport/screenshot tests against the fake apex (no Apex needed): visibility and style are
applied and put back, views and saved views, the capture guards (empty files, covered window) and
the overlay crop. Run from the venv:  python tests\\test_scene.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp", "worker"), os.path.join(HERE, "fakes")]

import apex  # noqa: E402  (the fake)
import apex_worker as W  # noqa: E402
import scene as SC  # noqa: E402
from PIL import Image  # noqa: E402

TMP = tempfile.mkdtemp(prefix="apexmcp_scene_")
W.set_inprocess({"output_dir": TMP})
P53, P54 = apex.P53, apex.P54
PATH53, PATH54 = P53.pathName, P54.pathName


def reset():
    for p in apex.PARTS:
        p.vis, p.color, p.render, p.transp = "Visible", [255, 255, 0], "-5", 0
        for s in p._so:
            s.vis = "Visible"
    apex.Solid.writes.clear()
    cam = apex.display.getCamera()
    cam.pos, cam.foc, cam.up, cam.scale, cam.calls = apex.Pt(10, 20, 30), apex.Pt(1, 2, 3), apex.Pt(0, 0, 1), 100.0, []
    apex.CAP["empty"] = 0
    apex.CALLS.clear()
    apex.TEXTS.clear()
    vdir = SC.views_dir(W)
    for f in os.listdir(vdir):
        os.remove(os.path.join(vdir, f))


def call(method, **params):
    r = W.handle_request({"id": 1, "method": method, "params": params})
    assert r["ok"], r.get("error")
    return r["result"]


def fails(method, **params):
    r = W.handle_request({"id": 1, "method": method, "params": params})
    assert not r["ok"], "expected an error, got %r" % (r.get("result"),)
    return r["error"]


def cam_tuple():
    c = apex.display.getCamera()
    return (c.pos.x, c.pos.y, c.pos.z, c.foc.x, c.foc.y, c.foc.z, c.up.z, c.scale)


class Spy:
    """Records what the scene looked like at the moment of each capture."""
    def __enter__(self):
        self.seen, self.orig = [], apex.display.captureImage

        def spy(**kw):
            self.seen.append({"vis": {p.pathName: p.vis for p in apex.PARTS},
                              "solids": [s.vis for s in P54._so], "colors": {p.pathName: list(p.color) for p in apex.PARTS},
                              "render": P53.render, "scale": apex.display.getCamera().scale})
            return self.orig(**kw)
        apex.display.captureImage = spy
        return self

    def __exit__(self, *a):
        apex.display.captureImage = self.orig


def test_plain_screenshot_crops_to_the_model_and_drops_the_panel():
    reset()
    r = call("screenshot")
    v = r["views"][0]
    assert v["cropped"] and v["size"][0] < 800 and v["size"][1] < 400, v
    im = Image.open(v["path"])
    assert im.size == tuple(v["size"])
    # the dark floating panel at the top-left of the full frame is not in the crop
    assert all(im.getpixel((x, y)) != (25, 25, 25) for x in range(0, im.width, 4) for y in range(0, im.height, 4))
    assert "sheet" not in r and r["restored"] and r["views"][0]["name"] == "current", r
    full = call("screenshot", crop="none")["views"][0]
    assert full["size"] == [800, 400] and not full["cropped"], full


def test_show_only_isolates_then_restores_parts_and_children():
    reset()
    P54._so[0].hide()  # the user had hidden one solid inside PID54 by hand
    with Spy() as spy:
        r = call("screenshot", scene={"show_only": [PATH54]})
    during = spy.seen[-1]
    assert during["vis"][PATH53] == "Hidden" and during["vis"][PATH54] == "Visible", during
    assert P54._so[0].vis == "Hidden" and all(s.vis == "Visible" for s in P54._so[1:]), [s.vis for s in P54._so]
    assert P54.vis == "NotSet" and P53.vis == "Visible", (P54.vis, P53.vis)  # as before: one solid hidden by hand
    assert "session.hideAll" not in apex.CALLS, "scenes must not use session.hideAll"
    assert r["applied"] == ["show_only"], r


def test_restore_only_writes_what_changed_so_plain_parts_stay_visible():
    reset()
    call("screenshot", scene={"show_only": [PATH53]})
    assert P53.vis == "Visible" and P54.vis == "Visible", (P53.vis, P54.vis)  # not NotSet
    call("screenshot", scene={"hide_all": True}, views=["iso1", "top"])
    assert P53.vis == "Visible" and P54.vis == "Visible", (P53.vis, P54.vis)
    # no child was touched individually after the cascades, so none is written back one by one
    assert apex.Solid.writes and all(v in ("Visible", "Hidden") for _, v in apex.Solid.writes)
    n = len(apex.Solid.writes)
    reset()
    call("screenshot", crop="none")  # no scene, no hidden frame: nothing to write at all
    assert len(apex.Solid.writes) == 0, apex.Solid.writes[:3]
    call("screenshot")  # crop=model hides and re-shows every part: cascades only (9 solids x 2), no per-child repair
    assert P54.vis == "Visible" and len(apex.Solid.writes) == 18, len(apex.Solid.writes)


def test_assemblies_with_a_definite_state_are_put_back_and_notset_ones_left_alone():
    reset()
    a_all, a_mixed = apex.Assembly("AsmAll", [P53]), apex.Assembly("AsmMixed", [P54])
    apex.ASSEMBLIES[:] = [a_all, a_mixed]
    try:
        P54._so[0].hide()  # AsmMixed is NotSet from the start: one solid inside was hidden by hand
        a_mixed.vis = "NotSet"  # as Apex shows it once something inside was changed by hand
        assert a_all.vis == "Visible" and a_mixed.vis == "NotSet", (a_all.vis, a_mixed.vis)  # set-up check
        call("screenshot", crop="none", scene={"show_only": [PATH54]})  # hides P53: AsmAll flips to NotSet
        assert a_all.vis == "Visible", "AsmAll was Visible and must come back Visible, not NotSet"
        assert a_mixed.vis == "NotSet" and P54._so[0].vis == "Hidden", (a_mixed.vis, P54._so[0].vis)
        assert P53.vis == "Visible"
        call("screenshot", scene={"hide_all": True}, views=["iso1", "top"])  # also through the reference frame
        assert a_all.vis == "Visible" and a_mixed.vis == "NotSet", (a_all.vis, a_mixed.vis)
    finally:
        apex.ASSEMBLIES[:] = []


def test_the_reference_frame_does_not_leave_hidden_solids_visible():
    reset()
    P54._so[2].hide()
    with Spy() as spy:
        call("screenshot", scene={"hide": [P54._so[1].pathName]})  # crop=model takes a hidden-parts frame first
    assert spy.seen[-1]["solids"][1] == "Hidden" and spy.seen[-1]["solids"][2] == "Hidden", spy.seen[-1]["solids"]
    assert P54._so[2].vis == "Hidden"


def test_style_is_applied_for_the_capture_and_restored():
    reset()
    with Spy() as spy:
        r = call("screenshot", scene={"style": {"color_by_part": True, "render_style": "Shaded",
                                                "transparency": {PATH53: 40}}})
    during = spy.seen[-1]
    assert during["colors"][PATH53] == SC.PALETTE[0] and during["colors"][PATH54] == SC.PALETTE[1], during
    assert during["render"] == "4"
    assert P53.color == [255, 255, 0] and P53.render == "-5" and P53.transp == 0, (P53.color, P53.render, P53.transp)
    assert P54.color == [255, 255, 0] and P54.render == "-5"
    assert r["warnings"] == [], r["warnings"]


def test_viewport_keeps_the_scene():
    reset()
    r = call("viewport", scene={"show_only": [PATH54], "style": {"mesh_edges": False, "color": {PATH54: [1, 2, 3]}}},
             view="front")
    assert P53.vis == "Hidden" and P54.vis == "Visible" and P54.color == [1, 2, 3], (P53.vis, P54.color)
    assert ("mesh_edges", False) in apex.CALLS and "camera" in r, (apex.CALLS, r)
    assert apex.display.getCamera().calls[-1][:3] == ("fit", "y", True)  # front = camera on -Y
    assert call("viewport")["camera"]["parallel_scale"] > 0  # nothing to do: reports the camera


def test_multi_view_sheet_and_camera_restore():
    reset()
    before = cam_tuple()
    r = call("screenshot", views=["iso1", "top", {"name": "close", "preset": "y", "zoom": 2}], max_width=900)
    assert [v["name"] for v in r["views"]] == ["iso1", "top", "close"], r["views"]
    assert os.path.isfile(r["sheet"]) and Image.open(r["sheet"]).width <= 900
    # Apex fits the preset; screenshots then zoom out to margin 0.85 (the fake divides the scale by the factor)
    scales = [v["camera"]["parallel_scale"] for v in r["views"]]
    assert abs(scales[0] - 120.0 / 0.85) < 1e-6 and abs(scales[1] - 200.0 / 0.85) < 1e-6, scales
    assert abs(scales[2] - 150.0 / (2 * 0.85)) < 1e-6, scales  # zoom multiplies with the margin
    assert cam_tuple() == before, "camera must be put back"
    assert call("screenshot", views=["iso1"], margin=1.0)["views"][0]["camera"]["parallel_scale"] == 120.0
    assert "margin" in fails("screenshot", margin=1.5)
    sep = call("screenshot", views=["iso1", "top"], layout="separate")
    assert "sheet" not in sep and len(sep["views"]) == 2


def test_bad_requests_change_nothing():
    reset()
    before = cam_tuple()
    e = fails("screenshot", scene={"show_only": [PATH54, "Model/NOPE"]}, views=["top"])
    assert "not found" in e and "Model/NOPE" in e, e
    assert P53.vis == "Visible" and cam_tuple() == before
    e = fails("screenshot", views=["sideways"])
    assert "unknown view 'sideways'" in e and "iso1" in e, e
    assert "color" in fails("screenshot", scene={"style": {"color": {PATH54: [1, 2]}}})
    assert "0-100" in fails("screenshot", scene={"style": {"transparency": {PATH54: 140}}})
    assert "show_only cannot be combined" in fails("viewport", scene={"show_only": [PATH54], "show_all": True})
    assert "at most 9" in fails("screenshot", views=["top"] * 10)
    assert "Wireframe" in fails("screenshot", scene={"style": {"render_style": "Shiny"}})
    assert "highlight needs" in fails("viewport", scene={"highlight": [{"color": [1, 2, 3]}]})
    assert P53.vis == "Visible" and cam_tuple() == before and not apex.CALLS


def test_empty_capture_is_retried_and_then_reported():
    reset()
    apex.CAP["empty"] = 1  # one 0-byte file, then a good one (Apex does this with its UI hidden)
    r = call("screenshot", crop="none")
    assert os.path.getsize(r["views"][0]["path"]) > 0
    apex.CAP["empty"] = 50
    e = fails("screenshot", crop="none", scene={"hide": [PATH53]})
    assert "empty file" in e and "UI hidden" in e, e
    assert P53.vis == "Visible", "scene must be restored even when the capture fails"
    apex.CAP["empty"] = 0


def test_saved_views_round_trip():
    reset()
    r = call("viewport", view={"preset": "iso2", "zoom": 1.5}, save_view="Corner A")
    saved = r["camera"]
    assert "Corner A" in r["saved_views"]
    call("viewport", view="top")
    assert cam_tuple()[-1] != saved["parallel_scale"]
    s = call("screenshot", views=[{"saved": "Corner A"}, "Corner A"], crop="none")
    assert s["views"][0]["camera"] == saved and s["views"][1]["camera"] == saved, (s["views"], saved)
    assert call("viewport", list_views=True)["saved_views"].keys() == {"Corner A"}
    assert "no saved view" in fails("screenshot", views=[{"saved": "nope"}])
    assert "view name" in fails("viewport", save_view="bad/name")
    assert call("viewport", delete_view="Corner A")["saved_views"] == {}
    assert "no saved view" in fails("viewport", delete_view="Corner A")


def test_things_without_readback_are_reported_not_silently_left():
    reset()
    r = call("screenshot", crop="none", scene={
        "style": {"mesh_edges": False}, "highlight": [{"paths": [PATH54]}],
        "labels": [{"text": "hello", "at": [0, 0, 0]}], "cut": {"location": [1, 2, 3]}})
    assert any("display toggles" in n for n in r["not_restored"]), r["not_restored"]
    assert any("cut view" in n for n in r["not_restored"]), r["not_restored"]
    assert apex.TEXTS == [], "labels we drew are removed again"
    assert "clearHighlights" in apex.CALLS and ("displayCutViews", False) in apex.CALLS, apex.CALLS
    kept = call("screenshot", crop="none", restore=False, scene={"labels": [{"text": "stay", "at": [0, 0, 0]}]})
    assert kept["restored"] is False and len(apex.TEXTS) == 1


def test_a_covered_window_is_raised_and_a_stuck_one_is_reported():
    reset()
    old = SC._win_probe, SC._win_raise
    states, raised = [{"hwnd": 1, "iconic": False, "covered_by": ["screenshots"]},
                      {"hwnd": 1, "iconic": False, "covered_by": []}], []
    SC._win_probe = lambda: states.pop(0) if len(states) > 1 else states[0]
    SC._win_raise = lambda hwnd: raised.append(hwnd)
    try:
        r = call("screenshot", crop="none")
        assert raised == [1] and r["window"] == {"checked": True, "brought_to_front": True}, (raised, r["window"])
        SC._win_probe = lambda: {"hwnd": 1, "iconic": False, "covered_by": ["Snip & Sketch"]}
        e = fails("screenshot", crop="none", scene={"hide": [PATH53]})
        assert "covered by ['Snip & Sketch']" in e and "screen pixels" in e, e
        assert P53.vis == "Visible", "nothing is changed when the window cannot be raised"
        W.CFG["screenshot_bring_to_front"] = False
        raised.clear()
        assert "screenshot_bring_to_front is false" in fails("screenshot", crop="none") and not raised
        SC._win_probe = lambda: {"hwnd": 1, "iconic": True, "covered_by": []}
        W.CFG["screenshot_bring_to_front"] = True
        SC._win_raise = lambda hwnd: None
        assert "minimized" in fails("screenshot", crop="none")
    finally:
        SC._win_probe, SC._win_raise = old
        W.CFG.pop("screenshot_bring_to_front", None)


def test_model_box_ignores_small_stray_differences():
    ref = Image.new("RGB", (800, 400), (40, 90, 160))
    im = ref.copy()
    for x in range(300, 600):
        for y in range(100, 300):
            im.putpixel((x, y), (200, 200, 0))
    for x in range(700, 706):  # a scale-bar tick / tree check mark
        for y in range(10, 16):
            im.putpixel((x, y), (255, 255, 255))
    x0, y0, x1, y1 = SC.model_box(ref, im)
    assert x0 <= 300 and x1 >= 600 and y0 <= 100 and y1 >= 300, (x0, y0, x1, y1)
    assert x1 < 700 and y0 > 16, (x0, y0, x1, y1)
    assert SC.model_box(ref, ref.copy()) is None
    assert SC.model_box(ref, Image.new("RGB", (10, 10))) is None


def test_methods_are_registered_and_the_old_viewport_tools_are_gone():
    assert {"viewport", "screenshot"} <= set(W.METHODS)
    for gone in ("visibility", "set_view", "display_options", "highlight", "annotate", "cut_view"):
        assert gone not in W.METHODS, gone
    assert "apex_mcp/worker/scene.py" in W.DEPLOYED_FILES


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for n, fn in tests:
        try:
            fn()
            print("ok  ", n)
        except Exception as exc:  # report every failure, not only the first
            import traceback
            failed += 1
            print("FAIL", n, "-", type(exc).__name__, exc)
            traceback.print_exc(limit=3)
    print("\nRESULT:", "PASS" if not failed else "FAIL (%d)" % failed)
    sys.exit(1 if failed else 0)
