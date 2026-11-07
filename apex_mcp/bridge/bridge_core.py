"""Localhost JSON-lines server used by the MCP Bridge palette tool.

Pure Python (no apex, no .NET) so it can be tested outside Apex.

Protocol (one client at a time, 127.0.0.1 only):
    client -> {"auth": "<token>"}
    server -> {"hello": "apex-bridge", "pid": <apex pid>, "version": 1}
              | {"error": "bad token"} | {"error": "another MCP client is connected ..."}
    client -> {"id": n, "method": "...", "params": {...}}
    server -> {"id": n, "ok": true, "result": ...} | {"id": n, "ok": false, "error": "..."}
`dispatch(request_dict) -> response_dict` does the actual work.

Every connection gets its own thread, so one that says nothing cannot block the next, and a
second client is refused at once instead of waiting in the listen queue. Requests sitting in
that queue would otherwise run when the first client leaves, after their sender has given up.
"""
import hmac
import itertools
import json
import os
import socket
import threading
import time
import traceback

VERSION = 1
AUTH_TIMEOUT_S = 5.0  # a connection must present the token within this time
NO_RESPONSE_GRACE_S = 3.0  # wait this long for the response file once Apex says the script ended
_SEQ = itertools.count(1)  # request file numbers, unique across dispatchers in this process


class BridgeServer:
    def __init__(self, port, token, dispatch, log=print, host="127.0.0.1", auth_timeout_s=AUTH_TIMEOUT_S):
        self.host, self.port, self.token = host, int(port), str(token)
        self.dispatch, self.log = dispatch, log
        self.auth_timeout_s = auth_timeout_s
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
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
        s.listen(5)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._loop, name="mcp-bridge", daemon=True)
        self._thread.start()
        self.log("listening on %s:%d" % (self.host, self.port))

    def stop(self):
        self._stop.set()
        client = self.client
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)  # wakes the reader; close() alone waits for it
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
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
            threading.Thread(target=self._handle, args=(conn,), name="mcp-bridge-client", daemon=True).start()

    def _handle(self, conn):
        rfile = wfile = None
        try:
            conn.settimeout(self.auth_timeout_s)
            rfile = conn.makefile("r", encoding="utf-8", newline="\n")
            wfile = conn.makefile("w", encoding="utf-8", newline="\n")
            self._serve(conn, rfile, wfile)
        except Exception as exc:  # pragma: no cover - logged, keep listening
            self.log("client error: %s" % exc)
        finally:
            with self._client_lock:
                was_client = self.client is conn
                if was_client:
                    self.client = None
            for obj in (rfile, wfile, conn):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            if was_client:
                self.log("client disconnected")

    def _authorized(self, line):
        try:
            auth = json.loads(line).get("auth")
            return isinstance(auth, str) and hmac.compare_digest(auth.encode(), self.token.encode())
        except Exception:
            return False

    def _serve(self, conn, rfile, wfile):
        def send(obj):
            wfile.write(json.dumps(obj, default=str) + "\n")
            wfile.flush()

        try:
            first = rfile.readline()
        except socket.timeout:
            self.log("dropped a connection that sent no token")
            return
        if not first:
            return
        if not self._authorized(first):
            send({"error": "bad token"})
            self.log("rejected a client with a wrong token")
            return
        with self._client_lock:
            free = self.client is None
            if free:
                self.client = conn
        if not free:
            send({"error": "another MCP client is connected to this Apex. The bridge serves one at a "
                           "time: close that client, then retry."})
            self.log("refused a second MCP client")
            return
        conn.settimeout(None)
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


def _remove(*files):
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass


def file_dispatch(queue_fn, work_dir, timeout_s=600, poll_s=0.05, grace_s=NO_RESPONSE_GRACE_S):
    """Build a dispatch() that hands each request to `queue_fn(req_file, resp_file)`
    (which must arrange for the bridge worker to run) and waits for the response file.
    queue_fn may return a threading.Event that is set when Apex reports the script
    finished; if it is set and no response appears, the call fails fast.

    Giving up on a call does not stop it: Apex keeps running it on its script thread. Until it
    ends, further calls are refused here, because Apex would queue them behind it."""
    os.makedirs(work_dir, exist_ok=True)
    lock = threading.Lock()
    overdue = {}  # the call that outlived timeout_s and may still be running

    def still_running():
        if not overdue:
            return False
        if os.path.exists(overdue["resp_file"]) or (overdue["finished"] is not None and overdue["finished"].is_set()):
            _remove(overdue["req_file"], overdue["resp_file"])
            overdue.clear()
            return False
        return True

    def dispatch(req):
        with lock:
            if still_running():
                return {"id": req.get("id"), "ok": False,
                        "error": "Apex is still running '%s' (started %.0fs ago), which outlived call_timeout_s. "
                                 "This call was not sent. Do not resend yet: wait for it to end. If Apex looks "
                                 "idle, look for an open dialog in Apex, or press Stop then Start in MCP Bridge."
                                 % (overdue["method"], time.time() - overdue["t0"])}
            stem = os.path.join(work_dir, "req_%d_%d" % (os.getpid(), next(_SEQ)))
            req_file, resp_file = stem + ".json", stem + ".resp.json"
            with open(req_file, "w", encoding="utf-8") as fh:
                json.dump(req, fh)
            t0 = time.time()
            finished = queue_fn(req_file, resp_file)
            t_end = t0 + float(timeout_s)
            grace_end = None
            while not os.path.exists(resp_file):
                now = time.time()
                if now > t_end:
                    overdue.update(method=req.get("method"), t0=t0, finished=finished,
                                   req_file=req_file, resp_file=resp_file)
                    return {"id": req.get("id"), "ok": False,
                            "error": "Apex did not finish '%s' within %ss. It may still be running: further "
                                     "calls are refused until it ends." % (req.get("method"), timeout_s)}
                if finished is not None and finished.is_set():
                    grace_end = grace_end or now + float(grace_s)
                    if now > grace_end:
                        _remove(req_file)
                        return {"id": req.get("id"), "ok": False,
                                "error": "Apex ran the bridge worker but it wrote no response for '%s'. The call "
                                         "may have run. Look for an open dialog in Apex; Apex's own report is "
                                         "in ScriptOutputMessage.log next to mcp_bridge_worker.py."
                                         % req.get("method")}
                time.sleep(poll_s)
            with open(resp_file, "r", encoding="utf-8") as fh:
                resp = json.load(fh)
            _remove(req_file, resp_file)
            return resp

    return dispatch
