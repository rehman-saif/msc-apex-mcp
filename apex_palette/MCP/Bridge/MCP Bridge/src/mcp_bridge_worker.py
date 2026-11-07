"""MCP Bridge - worker side. Runs on Apex's script thread via apex_sdk.runScriptFunction.

handle({"req_file", "resp_file", "root"}) reads one request, runs it through the
shared handlers in Apex_MCP/apex_mcp/worker/apex_worker.py with the in-process
`apex` module, and writes the response file atomically.

If apex_worker.py, builders.py, queries.py or serialize.py changed on disk since the last call,
they are reloaded first, so edits take effect without restarting Apex
(run_apex_python variables are reset when that happens).
"""
import importlib
import json
import os
import sys

_MODS = ("serialize", "queries", "builders", "apex_worker")
# Kept in sys.modules so it survives Apex re-executing this file for each call.
_ST = sys.modules.setdefault("_mcp_bridge_worker_state", type(sys)("_mcp_bridge_worker_state"))
if not hasattr(_ST, "mtimes"):
    _ST.mtimes = {}
_MTIMES = _ST.mtimes


def _arg(d, key):
    try:
        return str(d[key])
    except Exception:
        return str(dict(d)[key])


def _load(root):
    wdir = os.path.join(root, "apex_mcp", "worker")
    if wdir not in sys.path:
        sys.path.insert(0, wdir)
    stamp = {}
    for m in _MODS:
        try:
            stamp[m] = os.path.getmtime(os.path.join(wdir, m + ".py"))
        except OSError:
            stamp[m] = None
    # first call in this Apex session with older modules already imported counts as changed
    changed = stamp != _MTIMES and (bool(_MTIMES) or any(m in sys.modules for m in _MODS))
    for m in _MODS:
        if m in sys.modules and changed:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)
    _MTIMES.clear()
    _MTIMES.update(stamp)
    W = sys.modules["apex_worker"]
    config_path = os.path.join(root, "config.json")
    config_mtime = os.path.getmtime(config_path)
    if changed or not W.MODE["inprocess"] or config_mtime != getattr(_ST, "config_mtime", None):
        with open(config_path, "r", encoding="utf-8") as fh:
            W.set_inprocess(json.load(fh))
        _ST.config_mtime = config_mtime
        if changed:
            W.log("worker modules reloaded")
    return W


def _quiet():
    """Suppress Apex's script-output dialog. Without this Apex opens a modal output window
    after every runScriptFunction call, and the next call is blocked until it is closed."""
    try:
        import apex
        apex.disableShowOutput()
    except Exception:
        pass


def handle(d):
    _quiet()
    root = _arg(d, "root")
    req_file, resp_file = _arg(d, "req_file"), _arg(d, "resp_file")
    try:
        W = _load(root)
        with open(req_file, "r", encoding="utf-8") as fh:
            req = json.load(fh)
        resp = W.handle_request(req)
    except BaseException as exc:  # import/config problems; anything escaping opens a dialog in Apex
        import traceback
        resp = {"id": None, "ok": False, "error": "bridge worker: %s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc()[-4000:]}
    tmp = resp_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(resp, fh, default=str)
    os.replace(tmp, resp_file)
    _quiet()  # a run_apex_python script may have re-enabled it
    return ""
