"""Probe: can remoting attach to an Apex that getApplicationPids() does not list?

Run:  probe_attach.bat <apex_pid>      (Apex must be open; read-only, detaches at the end)
Report: output\\probe_report.txt
"""
import atexit
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LINES = []


def say(m):
    print(m, flush=True)
    LINES.append(str(m))


def timed(name, fn, timeout=60):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["e"] = "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc())

    t = threading.Thread(target=run, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout)
    dt = time.time() - t0
    if t.is_alive():
        say("[HANG] %s did not return within %ss" % (name, timeout))
        return None, "hang"
    if "e" in box:
        say("[FAIL] %s (%.1fs): %s" % (name, dt, box["e"]))
        return None, "error"
    say("[ OK ] %s (%.1fs): %r" % (name, dt, box.get("v")))
    return box.get("v"), "ok"


def main():
    pid = sys.argv[1] if len(sys.argv) > 1 else ""
    say("python %s, target pid=%r" % (sys.version.split()[0], pid))
    import remoting
    for hook in (remoting.shutdownApplication, remoting.terminate):
        atexit.unregister(hook)
    say("[ OK ] atexit shutdown hooks removed")
    timed("getApplicationPids", lambda: [str(p) for p in remoting.getApplicationPids()])
    timed("isAppRunning", remoting.isAppRunning)
    for k in ("MOD_DIR", "APPLICATION_TOP", "LEAF_DIR", "SCA_RESOURCE_DIR"):
        say("env %s=%s" % (k, os.environ.get(k, "")[:200]))
    if not pid:
        say("No pid given; stopping.")
        return
    _, st = timed("attachApplication(pid=%s)" % pid, lambda: remoting.attachApplication(pid=pid), timeout=90)
    if st != "ok":
        timed("attachApplication(pid=%s, debug=True)" % pid,
              lambda: remoting.attachApplication(pid=pid, debug=True), timeout=90)
    timed("getApplicationPids (after)", lambda: [str(p) for p in remoting.getApplicationPids()])
    timed("isAppRunning (after)", remoting.isAppRunning)
    apex, st2 = timed("import apex", lambda: __import__("apex"), timeout=120)
    if apex is not None:
        timed("getApplicationInfo", lambda: dict(apex.getApplicationInfo()))
        timed("currentModel().name", lambda: apex.currentModel().name)
        timed("part count", lambda: apex.currentModel().getParts(recursive=True).len())
    timed("detachApplication", remoting.detachApplication)


if __name__ == "__main__":
    try:
        main()
    finally:
        os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
        with open(os.path.join(HERE, "output", "probe_report.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(LINES))
        print("\nReport: output\\probe_report.txt")
        os._exit(0)  # skip any remaining exit handlers so nothing can shut Apex down
