"""TEST: runs her V8HandoffScheduler (unchanged) in its own process, fed by the same camera pipes."""
import os
import sys
import time


def serve(parent, socket_path, status_path, stop, here):
    if os.getenv("RULES_CPUS"):  # TEST: keep this process off the P-cores (0-5) so the engine's detector threads keep them
        a, b = os.getenv("RULES_CPUS").split("-")
        os.sched_setaffinity(0, set(range(int(a), int(b) + 1)))
    sys.path.insert(0, os.path.join(here, "v8code"))
    try:
        import requests  # noqa: F401
    except ImportError:
        import types
        sys.modules["requests"] = types.ModuleType("requests")
    os.environ["SENTINEL_CV_HANDOFF_STATUS_PATH"] = status_path
    from pipeline import sentinel_v8_handoff as hv
    from pipeline.sentinel_v8_handoff import V8HandoffScheduler
    q = int(os.getenv("HANDOFF_QUEUE") or 0)
    if q > 0:  # PRODUCTION POLICY (contract: queue_package_limit is CV tuning, not contract); in-flight stays 4
        hv.MAX_RETAINED_PACKAGES, hv.MAX_REPLACEABLE_PACKAGES = q, max(1, q // 2)
        print(f"{time.strftime('%T')} handoff queue: {hv.MAX_RETAINED_PACKAGES} packages ({hv.MAX_REPLACEABLE_PACKAGES} replaceable), "
              f"in flight {hv.MAX_IN_FLIGHT}", flush=True)
    if os.getenv("PKG_SHM") == "1":  # TEST: JPEG bytes arrive via shared memory; multipart sent piecewise (no joined copy)
        import pkgshm
        parent = {k: pkgshm.HostConn(v) for k, v in parent.items()}
        pkgshm.patch_multipart(hv)
        print(f"{time.strftime('%T')} handoff: package bytes via shared memory", flush=True)
    scheduler = V8HandoffScheduler(parent, socket_path, status_path).start()
    print(f"{time.strftime('%T')} handoff process ready (pid {os.getpid()})", flush=True)
    while not stop.wait(1.0):
        pass
    scheduler.close()
    print(f"{time.strftime('%T')} handoff process stopped", flush=True)
