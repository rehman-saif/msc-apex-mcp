def getApplicationPids(): return ["4242"]
def attachApplication(pid="", debug=False): print("fake attach", pid)
def detachApplication(): print("fake detach")
def shutdownApplication(): raise SystemExit("SHUTDOWN CALLED - must never happen")
def terminate(): pass
import atexit; atexit.register(shutdownApplication)
