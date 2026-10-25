"""Localhost JSON-lines server used by the MCP Bridge palette tool.

Pure Python (no apex, no .NET) so it can be tested outside Apex.

Protocol (one client at a time, 127.0.0.1 only):
    client -> {"auth": "<token>"}
    server -> {"hello": "apex-bridge", "pid": <apex pid>, "version": 1}
    client -> {"id": n, "method": "...", "params": {...}}
    server -> {"id": n, "ok": true, "result": ...} | {"id": n, "ok": false, "error": "..."}
`dispatch(request_dict) -> response_dict` does the actual work.
"""
import json
import os
import socket
import threading
import time
import traceback

VERSION = 1


class BridgeServer:
    def __init__(self, port, token, dispatch, log=print, host="127.0.0.1"):
        self.host, self.port, self.token = host, int(port), str(token)
        self.dispatch, self.log = dispatch, log
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.client = None
        self.calls = 0
        self.last = ""

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))  # raises if the port is taken
        s.listen(1)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._loop, name="mcp-bridge", daemon=True)
        self._thread.start()
        self.log("listening on %s:%d" % (self.host, self.port))

    def stop(self):
        self._stop.set()
        for obj in (self.client, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self.client = None
        self.log("stopped")

    # ------------------------------------------------------------ internals
    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if addr[0] != "127.0.0.1":
                conn.close()
                continue
            self.client = conn
            try:
                self._serve(conn)
            except Exception as exc:  # pragma: no cover - logged, keep listening
                self.log("client error: %s" % exc)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                self.client = None
                self.log("client disconnected")

    def _serve(self, conn):
        conn.settimeout(None)
        rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        wfile = conn.makefile("w", encoding="utf-8", newline="\n")

        def send(obj):
            wfile.write(json.dumps(obj, default=str) + "\n")
            wfile.flush()

        first = rfile.readline()
        try:
            auth = json.loads(first).get("auth")
        except Exception:
            auth = None
        if auth != self.token:
            send({"error": "bad token"})
            self.log("rejected a client with a wrong token")
            return
        send({"hello": "apex-bridge", "pid": os.getpid(), "version": VERSION})
        self.log("client connected")
        for line in rfile:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                send({"ok": False, "error": "bad json"})
                continue
            t0 = time.time()
            method = req.get("method")
            try:
                resp = self.dispatch(req)
            except Exception as exc:
                resp = {"id": req.get("id"), "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                        "traceback": traceback.format_exc()[-4000:]}
            self.calls += 1
            self.last = "%s %s %.1fs" % (method, "ok" if resp.get("ok") else "ERROR", time.time() - t0)
            self.log(self.last if resp.get("ok") else "%s: %s" % (self.last, str(resp.get("error"))[:200]))
            send(resp)


def file_dispatch(queue_fn, work_dir, timeout_s=600, poll_s=0.05):
    """Build a dispatch() that hands each request to `queue_fn(req_file, resp_file)`
    (which must arrange for the bridge worker to run) and waits for the response file.
    queue_fn may return a threading.Event that is set when Apex reports the script
    finished; if it is set and no response appears, the call fails fast."""
    os.makedirs(work_dir, exist_ok=True)
    counter = {"n": 0}
    lock = threading.Lock()

    def dispatch(req):
        with lock:
            counter["n"] += 1
            stem = os.path.join(work_dir, "req_%d_%d" % (os.getpid(), counter["n"]))
            req_file, resp_file = stem + ".json", stem + ".resp.json"
            with open(req_file, "w", encoding="utf-8") as fh:
                json.dump(req, fh)
            if os.path.exists(resp_file):
                os.remove(resp_file)
            finished = queue_fn(req_file, resp_file)
            t_end = time.time() + float(timeout_s)
            grace_end = None
            while not os.path.exists(resp_file):
                now = time.time()
                if now > t_end:
                    return {"id": req.get("id"), "ok": False,
                            "error": "Apex did not finish '%s' within %ss" % (req.get("method"), timeout_s)}
                if finished is not None and finished.is_set():
                    grace_end = grace_end or now + 3.0
                    if now > grace_end:
                        return {"id": req.get("id"), "ok": False,
                                "error": "Apex ran the bridge worker but it wrote no response for '%s' "
                                         "(check the Apex output window / bridge_worker.log)" % req.get("method")}
                time.sleep(poll_s)
            with open(resp_file, "r", encoding="utf-8") as fh:
                resp = json.load(fh)
            for f in (req_file, resp_file):
                try:
                    os.remove(f)
                except Exception:
                    pass
            return resp

    return dispatch
