"""Smoke test for the Apex bridge. Run with APEX'S Python while Apex is open:

    run_smoke_test.bat            (or)
    "<Apex>\\python3\\python.exe" smoke_test.py [apex_pid] [path\\to\\results.h5]

It checks, step by step, that this process can attach to the open Apex window
and use the API the MCP tools rely on. It changes nothing in the model.
It only reads, takes one screenshot, and then detaches.
The report is printed and saved to output\\smoke_report.txt.
"""
import atexit
import datetime
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output", "smoke")
os.makedirs(OUT, exist_ok=True)
REPORT = []


def say(msg):
    print(msg, flush=True)
    REPORT.append(msg)


def step(name, fn):
    try:
        val = fn()
        say("[ OK ] %s: %s" % (name, val))
        return val
    except Exception as exc:
        say("[FAIL] %s: %s: %s" % (name, type(exc).__name__, exc))
        REPORT.append(traceback.format_exc())
        return None


def main():
    args = [a for a in sys.argv[1:]]
    pid_arg = next((a for a in args if a.isdigit()), None)
    h5_arg = next((a for a in args if a.lower().endswith(".h5")), None)
    say("Apex bridge smoke test  %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    say("python: %s  (%s)" % (sys.version.split()[0], sys.executable))

    remoting = step("import remoting", lambda: __import__("remoting"))
    if remoting is None:
        return
    # remoting registers shutdownApplication() at exit -> would close YOUR Apex. Remove it.
    for hook in (getattr(remoting, "shutdownApplication", None), getattr(remoting, "terminate", None)):
        if hook is not None:
            atexit.unregister(hook)
    say("[ OK ] removed remoting's atexit shutdown hooks")

    pids = step("getApplicationPids", lambda: [str(p) for p in remoting.getApplicationPids()])
    if not pids:
        say("No running Apex found. Open Apex (with any model), then run this again.")
        return
    pid = pid_arg or pids[0]
    if len(pids) > 1 and not pid_arg:
        say("Several Apex instances %s; using %s (pass a PID to choose)" % (pids, pid))

    attached = step("attachApplication(pid=%s)" % pid, lambda: remoting.attachApplication(pid=pid) or "returned")
    if attached is None:
        return
    try:
        apex = step("import apex", lambda: __import__("apex"))
        if apex is None:
            return
        step("getApplicationInfo", lambda: dict(apex.getApplicationInfo()))
        model = step("currentModel", lambda: apex.currentModel())
        if model is not None:
            step("model name / pathName", lambda: (model.name, model.pathName))
        step("script units", apex.getScriptUnitSystemLabel)
        step("application units", apex.getApplicationUnitSystemLabel)
        parts = step("getParts(recursive=True) count",
                     lambda: model.getParts(recursive=True).len() if model is not None else None)
        if parts:
            step("first 10 parts", lambda: [(p.name, p.pathName) for p in list(model.getParts(recursive=True))[:10]])
            first = list(model.getParts(recursive=True))[0]
            step("first part element count", lambda: first.elements.len())
            step("first part bounding box", lambda: (lambda b: ((b.point1.x, b.point1.y, b.point1.z),
                                                               (b.point2.x, b.point2.y, b.point2.z)))(
                first.getAxisAlignedBoundingBox()))
            step("getEntities(pathNames=[first part])", lambda: apex.getEntities(pathNames=[first.pathName]).len())
        study = step("getPrimaryStudy", lambda: apex.getPrimaryStudy())
        if study is not None:
            step("scenarios", lambda: [s.name for s in study.getScenarios(recursive=True)])
            step("nastran scenarios", lambda: [s.name for s in study.getScenariosNastran()])

            def events():
                out = []
                for s in study.getScenarios(recursive=True):
                    for holder in (s, getattr(s, "lastExecutedScenario", None)):
                        if holder is None:
                            continue
                        for st in holder.steps:
                            for ev in st.getEvents():
                                out.append((ev.pathName, ev.resultsDataAvailable))
                return out
            step("events (pathName, resultsDataAvailable)", events)
        for name in ("getFasteners", "getRigidLinks", "getBushings", "getConnectors", "getInteractions"):
            fn = getattr(apex.attribute, name)
            step("%s(target=[{'path': model}]) count" % name,
                 lambda fn=fn: fn(target=[{"path": model.pathName}]).len())
        step("environment.getLoads count", lambda: apex.environment.getLoads().len())
        step("camera focal point", lambda: (lambda c: (c.x, c.y, c.z))(apex.display.getCamera().getFocalPoint()))
        shot = step("captureImage (png)", lambda: apex.display.captureImage(
            path=OUT, imageNamePrefix="smoke", imageCaptureRegion=apex.display.CaptureRegionType.Viewport,
            imageFormat=apex.display.ImageType.png))
        step("files in output/smoke", lambda: os.listdir(OUT))
        _ = shot
    finally:
        step("detachApplication", lambda: remoting.detachApplication() or "detached")

    if h5_arg:
        sys.path.insert(0, os.path.join(HERE, "apex_mcp", "worker"))
        import h5results as H
        inv = step("h5 inventory", lambda: "%d result datasets" % len(H.inventory(h5_arg)["result_datasets"]))
        if inv:
            for d in H.inventory(h5_arg)["result_datasets"][:40]:
                say("   %s  rows=%s  fields=%s" % (d["path"], d["rows"], ",".join(d["fields"][:14])))
            step("h5 EID->PID source", lambda: H.eid_to_pid(h5_arg)[1])


if __name__ == "__main__":
    try:
        main()
    finally:
        path = os.path.join(HERE, "output", "smoke_report.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(REPORT))
        print("\nReport saved to %s" % path)
