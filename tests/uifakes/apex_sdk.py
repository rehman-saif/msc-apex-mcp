"""Fake apex_sdk: runScriptFunction runs the worker function on a separate 'Apex script' thread."""
import functools, importlib.util, threading, traceback, queue
_q = queue.Queue()
def _loop():
    while True:
        file, fn, args, cb = _q.get()
        try:
            spec = importlib.util.spec_from_file_location("bridge_worker_mod", file)
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            getattr(m, fn)({str(k): str(v) for k, v in args.items()})
        except Exception:
            traceback.print_exc()
        if cb: cb()
threading.Thread(target=_loop, daemon=True).start()
def runScriptFunction(file, function, args={}, callback=None): _q.put((file, function, args, callback))
def errorhandler(f):
    @functools.wraps(f)
    def w(*a, **k):
        try: return f(*a, **k)
        except Exception: traceback.print_exc()
    return w
class ToolPropertyContainer: pass
