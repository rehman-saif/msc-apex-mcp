"""MCP Bridge - Apex custom tool (UI side).

Press Start: a listener on 127.0.0.1:<bridge_port> accepts the Apex MCP server
(token from config.json). Each request is handed to Apex's script thread with
apex_sdk.runScriptFunction -> mcp_bridge_worker.handle, and the answer goes back.

This file runs in the Apex UI process: it never imports `apex` and never does
model work itself. Closing the panel does not stop the bridge; use Stop.
"""
import datetime
import json
import os
import sys
import threading
import traceback

import apex_sdk
import clr  # noqa: F401  (pythonnet, loaded by Apex)
import System
import System.Windows.Controls as WPF
from System.Windows import Thickness, TextWrapping

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "mcp_bridge_worker.py")


def _find_root(start):
    d = start
    for _ in range(8):
        if os.path.isfile(os.path.join(d, "config.json")) and os.path.isdir(os.path.join(d, "apex_mcp")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("Could not find Apex_MCP (config.json) above %s" % start)


ROOT = _find_root(HERE)
if os.path.join(ROOT, "apex_mcp", "bridge") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "apex_mcp", "bridge"))
import bridge_core  # noqa: E402


def _cfg():
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _version():
    with open(os.path.join(ROOT, "apex_mcp", "version.py"), "r", encoding="utf-8") as fh:
        return fh.read().split('__version__ = "')[1].split('"')[0]


# State survives closing/reopening the panel (module may be re-imported).
_ST = sys.modules.setdefault("_mcp_bridge_state", type(sys)("_mcp_bridge_state"))
if not hasattr(_ST, "server"):
    _ST.server = None
    _ST.lines = []
    _ST.widgets = {}


# ------------------------------------------------------------------ UI helpers
def _ui(fn):
    """Run fn on the WPF UI thread (synchronously)."""
    app = System.Windows.Application.Current
    if app is None or app.Dispatcher.CheckAccess():
        return fn()
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as exc:  # pragma: no cover
            box["e"] = exc
    app.Dispatcher.Invoke(System.Action(run))
    if "e" in box:
        raise box["e"]
    return box.get("v")


def log(msg):
    line = "%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    _ST.lines = (_ST.lines + [line])[-300:]
    try:
        out = os.path.join(_cfg().get("output_dir", os.path.join(ROOT, "output")), "bridge")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "bridge_ui.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

    def upd():
        w = _ST.widgets
        if "log" in w:
            w["log"].Text = "\n".join(_ST.lines[-120:])
            w["log"].ScrollToEnd()
        if "status" in w:
            w["status"].Text = _status_text()
    try:
        app = System.Windows.Application.Current
        if app is not None:
            app.Dispatcher.BeginInvoke(System.Action(upd))
    except Exception:
        pass


def _status_text():
    s = _ST.server
    if s is None or not s.running:
        return "Stopped"
    who = "MCP client connected" if s.client is not None else "waiting for MCP client"
    return "Listening on 127.0.0.1:%d  -  %s  -  calls: %d  %s" % (s.port, who, s.calls,
                                                                  ("(last: %s)" % s.last) if s.last else "")


# ------------------------------------------------------------- dispatching
def _queue_on_apex(req_file, resp_file):
    args = {"req_file": req_file, "resp_file": resp_file, "root": ROOT}
    finished = threading.Event()

    def go():
        apex_sdk.runScriptFunction(WORKER, "handle", args, callback=finished.set)
    _ui(go)
    return finished


def _make_dispatch():
    cfg = _cfg()
    work = os.path.join(cfg.get("output_dir", os.path.join(ROOT, "output")), "bridge", "queue")
    return bridge_core.file_dispatch(_queue_on_apex, work, timeout_s=float(cfg.get("call_timeout_s", 600)))


# ---------------------------------------------------------------- handlers
@apex_sdk.errorhandler
def on_start(sender=None, args=None):
    if _ST.server is not None and _ST.server.running:
        log("already running")
        return
    cfg = _cfg()
    port, token = int(cfg.get("bridge_port", 47115)), cfg.get("bridge_token")
    if not token:
        raise RuntimeError("bridge_token missing in config.json")
    _ST.server = bridge_core.BridgeServer(port, token, _make_dispatch(), log=log)
    _ST.server.start()


@apex_sdk.errorhandler
def on_stop(sender=None, args=None):
    if _ST.server is not None:
        _ST.server.stop()
    _ST.server = None
    log("bridge off")


@apex_sdk.errorhandler
def on_selftest(sender=None, args=None):
    # share the running server's dispatch: it knows whether a timed-out call is still in Apex
    running = _ST.server is not None and _ST.server.running
    dispatch = _ST.server.dispatch if running else _make_dispatch()

    def run():
        try:
            log("self-test: status ...")
            r = dispatch({"id": "t1", "method": "status", "params": {}})
            if not r.get("ok"):
                log("self-test FAILED at status: %s" % r.get("error"))
                return
            res = r["result"]
            log("  Apex %s | model %s | units %s" % (
                (res.get("application") or {}).get("release"), (res.get("model") or {}).get("name"),
                res.get("script_units")))
            r = dispatch({"id": "t2", "method": "list_tree", "params": {"counts": False}})
            if r.get("ok"):
                log("  top-level parts: %d" % len(r["result"].get("parts", [])))
            else:
                log("  list_tree FAILED: %s" % r.get("error"))
            r = dispatch({"id": "t3", "method": "screenshot", "params": {"max_width": 800}})
            res = r.get("result") or {}
            shot = res.get("sheet") or ((res.get("views") or [{}])[0].get("path"))
            log("  screenshot: %s" % (shot if r.get("ok") else "FAILED " + str(r.get("error"))))
            log("  version %s" % _version())
            log("self-test done")
        except Exception:
            log("self-test crashed:\n" + traceback.format_exc())
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- the panel
def _button(text, handler):
    b = WPF.Button()
    b.Content = text
    b.Margin = Thickness(0, 0, 6, 0)
    b.Padding = Thickness(10, 3, 10, 3)
    b.Click += handler
    return b


def getCustomToolPropertyContent():
    panel = WPF.StackPanel()
    panel.Margin = Thickness(6)

    title = WPF.TextBlock()
    title.Text = "Lets an MCP client (via the Apex MCP server) work in this Apex session."
    title.TextWrapping = TextWrapping.Wrap
    panel.Children.Add(title)

    status = WPF.TextBlock()
    status.Margin = Thickness(0, 6, 0, 6)
    status.TextWrapping = TextWrapping.Wrap
    status.Text = _status_text()
    panel.Children.Add(status)

    row = WPF.StackPanel()
    row.Orientation = WPF.Orientation.Horizontal
    row.Children.Add(_button("Start", on_start))
    row.Children.Add(_button("Stop", on_stop))
    row.Children.Add(_button("Self-test", on_selftest))
    panel.Children.Add(row)

    box = WPF.TextBox()
    box.Margin = Thickness(0, 8, 0, 0)
    box.Height = 260
    box.IsReadOnly = True
    box.TextWrapping = TextWrapping.Wrap
    box.VerticalScrollBarVisibility = WPF.ScrollBarVisibility.Auto
    box.FontFamily = System.Windows.Media.FontFamily("Consolas")
    box.Text = "\n".join(_ST.lines[-120:])
    panel.Children.Add(box)

    _ST.widgets = {"status": status, "log": box}
    return panel


def getUIContent():
    tp = apex_sdk.ToolPropertyContainer()
    tp.TitleText = "MCP Bridge"
    tp.IsCustomTool = True
    tp.IsObjectPropertyControl = False
    tp.IsAppliedVisible = False
    tp.ShowPickChoice = False
    tp.ToolPropertyContent = getCustomToolPropertyContent()
    return tp
