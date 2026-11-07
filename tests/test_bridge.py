"""Connection and queueing tests of the bridge, with the real BridgeServer and the real MCP
server's Bridge client but no Apex. Run from the venv:  python tests\\test_bridge.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "apex_mcp"), os.path.join(ROOT, "apex_mcp", "bridge"),
                os.path.join(ROOT, "apex_mcp", "worker")]
TMP = tempfile.mkdtemp(prefix="apexmcp_bridge_")
os.environ["APEX_MCP_CONFIG"] = os.path.join(TMP, "config.json")

import bridge_core  # noqa: E402
import server  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fixture:
    """A running BridgeServer that records what it ran, and a config pointing clients at it."""

    def __init__(self, on_call=None, **kw):
        self.port = free_port()
        with open(server.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"bridge_port": self.port, "bridge_token": "t", "call_timeout_s": 5}, fh)
        self.ran = []
        self.on_call = on_call
        self.server = bridge_core.BridgeServer(self.port, "t", self.dispatch, log=lambda m: None, **kw)
        self.server.start()

    def dispatch(self, req):
        self.ran.append(req["method"])
        if self.on_call:
            self.on_call(self)
        return {"id": req["id"], "ok": True, "result": "ok"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.stop()


def failure(fn, *a):
    try:
        fn(*a)
    except Exception as exc:
        return str(exc)
    raise AssertionError("expected %s%r to fail" % (fn.__name__, a))


def wait_until(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_second_client_is_refused_clearly_and_at_once():
    with Fixture() as f:
        a, b = server.Bridge(), server.Bridge()
        assert a.call("first") == "ok"
        t0 = time.time()
        msg = failure(b.call, "second")
        assert time.time() - t0 < 2, "took %.1fs" % (time.time() - t0)
        assert "another MCP client" in msg and "press Start" not in msg, msg
        a.close()
        assert wait_until(lambda: f.server.client is None)
        assert b.call("second again") == "ok", "the bridge must be free once the first client left"


def test_a_refused_call_never_runs_later():
    with Fixture() as f:
        a, b = server.Bridge(), server.Bridge()
        a.call("first")
        for i in (1, 2):
            failure(b.call, "refused_%d" % i)
        b.close()
        a.close()
        time.sleep(0.5)
        assert f.ran == ["first"], f.ran


def test_silent_connection_does_not_lock_out_the_client():
    with Fixture() as f:
        idle = socket.create_connection(("127.0.0.1", f.port))
        time.sleep(0.1)
        t0 = time.time()
        assert server.Bridge().call("real") == "ok"
        assert time.time() - t0 < 2
        idle.close()


def test_silent_connection_is_dropped_after_the_auth_timeout():
    with Fixture(auth_timeout_s=0.2) as f:
        idle = socket.create_connection(("127.0.0.1", f.port))
        idle.settimeout(3)
        assert idle.recv(1) == b"", "the bridge should have closed it"
        idle.close()


def test_wrong_token_is_rejected():
    with Fixture() as f:
        with socket.create_connection(("127.0.0.1", f.port), timeout=3) as s:
            s.sendall(b'{"auth": "nope"}\n{"id": 1, "method": "evil"}\n')
            assert b"bad token" in s.recv(200)
        time.sleep(0.2)
        assert f.ran == [], f.ran
        c = server.Bridge()
        with open(server.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"bridge_port": f.port, "bridge_token": "wrong"}, fh)
        assert "bridge_token" in failure(c.call, "x")


def test_lost_connection_is_reported_and_not_resent():
    def drop(fx):
        fx.server.client.shutdown(socket.SHUT_RDWR)
    with Fixture(on_call=drop) as f:
        msg = failure(server.Bridge().call, "changes_the_model")
        assert f.ran == ["changes_the_model"], "ran twice: %s" % f.ran
        assert "not resent" in msg, msg


def test_restarted_bridge_reconnects_without_an_error():
    with Fixture() as f:
        c = server.Bridge()
        assert c.call("before") == "ok"
        f.server.stop()
        f.server.start()
        assert c.call("after") == "ok"
        assert f.ran == ["before", "after"], f.ran


def test_stop_disconnects_the_client_at_once():
    with Fixture() as f:
        c = server.Bridge()
        c.call("x")
        f.server.stop()
        assert "Cannot reach" in failure(c.call, "y")


# -------------------------------------------------------------------- calls queued on Apex
class SlowApex:
    """Stands in for apex_sdk.runScriptFunction: runs each call on a thread, one at a time."""

    def __init__(self, seconds, respond=True):
        self.seconds, self.respond = seconds, respond
        self.started = []
        self.gate = threading.Lock()

    def __call__(self, req_file, resp_file):
        finished = threading.Event()
        self.started.append(req_file)

        def run():
            with self.gate:
                time.sleep(self.seconds)
                if self.respond:
                    with open(resp_file + ".tmp", "w", encoding="utf-8") as fh:
                        json.dump({"ok": True, "result": "done"}, fh)
                    os.replace(resp_file + ".tmp", resp_file)
                finished.set()
        threading.Thread(target=run, daemon=True).start()
        return finished


def queue_dir():
    return tempfile.mkdtemp(dir=TMP)


def test_call_after_a_timeout_is_refused_while_apex_is_still_running():
    apex = SlowApex(1.0)
    d = bridge_core.file_dispatch(apex, queue_dir(), timeout_s=0.2)
    assert "did not finish" in d({"id": 1, "method": "slow"})["error"]
    t0 = time.time()
    r = d({"id": 2, "method": "next"})
    assert time.time() - t0 < 0.2 and not r["ok"], r
    assert "still running 'slow'" in r["error"], r
    assert len(apex.started) == 1, "the refused call reached Apex: %s" % apex.started


def test_calls_work_again_once_the_timed_out_call_ends():
    work = queue_dir()
    apex = SlowApex(0.4)
    d = bridge_core.file_dispatch(apex, work, timeout_s=0.1)
    d({"id": 1, "method": "slow"})
    time.sleep(0.6)
    apex.seconds = 0.0
    assert d({"id": 2, "method": "next"})["ok"]
    assert os.listdir(work) == [], "left behind: %s" % os.listdir(work)


def test_call_without_a_response_leaves_no_files_and_names_the_dialog():
    work = queue_dir()
    d = bridge_core.file_dispatch(SlowApex(0.0, respond=False), work, timeout_s=9, grace_s=0.1)
    r = d({"id": 1, "method": "m"})
    assert not r["ok"] and "dialog" in r["error"], r
    assert os.listdir(work) == [], "left behind: %s" % os.listdir(work)


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
    os._exit(1 if failed else 0)  # a timed-out fake call may still be sleeping on its thread
