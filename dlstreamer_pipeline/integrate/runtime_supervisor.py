#!/usr/bin/env python3
"""CV runtime supervisor: the contract's desired-state handling and the CV health endpoints, around integrate9.py.

Contract (sentry-v8-contract.yaml, desiredState): revision strictly_increasing; validate_whole_revision_then_atomically_apply;
invalid: keep_last_valid_and_report_not_ready. Same behaviour as Kiranmai's solution_image_entrypoint.py:

- Every DESIRED_STATE_RELOAD_INTERVAL s (default 2) the desired-state file is read. A changed file is copied, the copy is
  validated whole (her DesiredStateValidator), and its revision must be greater than the active one.
- Same cameras / sources / fps / apps / embedding profiles (her _hot_reload_signature): the validated copy replaces the
  engine's applied file at once; the running engine passes each camera's new config to her V8EventPipeline.reconfigure().
- Anything else: the engine is stopped (it finishes its packages' handoff on the way out) and started with the new
  revision. If it does not come up within WORKER_CAMERA_INITIALIZATION_SECONDS, the last valid revision is started again.
- A rejected revision leaves the running one untouched and makes /readyz report not ready until a valid revision arrives.
- An engine that exits on its own is restarted with the active revision (bounded backoff).
- GET /healthz (200 while this process runs, with details) and /readyz (200 ready / 503 not ready, per-camera state) on
  APEX_API_HOST:APEX_API_PORT (default 0.0.0.0:8080).

Usage: runtime_supervisor.py DESIRED_STATE.json [duration_s]   (same arguments as integrate9.py)
"""
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "v8code"))
import desired_runtime  # noqa: E402
from traffic_pilot_runtime.desired_state import file_hash  # noqa: E402

def env(name, default):
    """Setting from the environment; empty counts as not set (run_integrate.sh passes unset ones as empty strings)."""
    return os.getenv(name) or default


SOURCE = Path(sys.argv[1]).resolve()
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 0
INTERVAL = float(env("DESIRED_STATE_RELOAD_INTERVAL", "2"))
START_S = float(env("WORKER_CAMERA_INITIALIZATION_SECONDS", "180"))
STOP_S = float(env("ENGINE_STOP_SECONDS", "120"))
HOT_ACK_S = float(env("HOT_RELOAD_ACK_SECONDS", "15"))
STATE_DIR = Path(env("APEXFABRIC_STATE_ROOT", "/tmp")) / "runtime"
APPLIED = STATE_DIR / "desired_state.applied.json"
CANDIDATE = STATE_DIR / "desired_state.candidate.json"
STATUS = STATE_DIR / "engine_status.json"
ENGINE = os.path.join(HERE, env("ENGINE_SCRIPT", "integrate9.py"))

lock = threading.Lock()
state = {"ready": False, "worker_running": False, "engine_pid": None, "active_revision": None, "active_hash": None,
         "pending_revision": None, "reload_state": "idle", "latest_error": "", "applied_count": 0, "hot_count": 0,
         "rejected_count": 0, "engine_starts": 0, "unexpected_exits": 0, "last_reload_at": None, "events": []}
stop = threading.Event()


def log(text):
    print(f"{time.strftime('%T')} supervisor: {text}", flush=True)


def event(kind, **payload):
    with lock:
        state["events"] = (state["events"] + [{"at": time.time(), "event": kind, **payload}])[-50:]
    log(f"{kind} {payload}")


def engine_status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None


class Engine:
    def __init__(self):
        self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Starts integrate9.py on the applied file; True once its pipeline is running (it writes its status every
        second from the main loop). Camera connectivity is not required (contract runtimeAvailability.startup);
        each camera's state is reported separately in /readyz."""
        try:
            STATUS.unlink()
        except FileNotFoundError:
            pass
        env = dict(os.environ, RUNTIME_STATUS_PATH=str(STATUS))
        # own process group: everything the engine starts (event processes, model server, handoff, multiprocessing
        # helpers) is stopped with it
        self.proc = subprocess.Popen([sys.executable, "-s", ENGINE, str(APPLIED), "0"], env=env, cwd=HERE,
                                     start_new_session=True)
        with lock:
            state["engine_starts"] += 1
            state["engine_pid"], state["worker_running"] = self.proc.pid, True
        log(f"engine started, pid {self.proc.pid}")
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline and not stop.is_set():
            if self.proc.poll() is not None:
                log(f"engine exited during start-up, code {self.proc.returncode}")
                self._reap()
                return False
            st = engine_status()
            if st and st.get("pid") == self.proc.pid:
                seen = sum(c["seen"] for c in st["cameras"].values())
                log(f"engine pid {self.proc.pid} running, {seen}/{len(st['cameras'])} cameras with frames so far")
                return True
            time.sleep(0.5)
        log("engine pipeline did not start before the start-up deadline")
        return False

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            log(f"stopping engine pid {self.proc.pid}")
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=STOP_S)
            except subprocess.TimeoutExpired:
                log("engine did not stop in time, killing it")
                self.proc.kill()
                self.proc.wait()
        self._reap()
        with lock:
            state["worker_running"], state["engine_pid"] = False, None

    def _reap(self):
        """Stops whatever is left in the engine's process group (e.g. multiprocessing's resource tracker)."""
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            time.sleep(1)
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


engine = Engine()
active = {"desired": None}


def reject(reason, observed_hash):
    with lock:
        state.update(ready=False, reload_state="rejected", latest_error=reason, pending_revision=None,
                     last_reload_at=time.time())
        state["rejected_count"] += 1
        state["rejected_hash"] = observed_hash
    event("desired_state_rejected", reason=reason)


def try_reload():
    try:
        observed = file_hash(SOURCE)
    except OSError as exc:
        if state.get("rejected_hash") != "unreadable":
            reject(f"desired-state file is unreadable: {exc}", "unreadable")
        return
    with lock:
        if observed in (state["active_hash"], state.get("rejected_hash")):
            return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, CANDIDATE)  # the exact bytes that are validated are the bytes that get applied
    try:
        desired = desired_runtime.load(CANDIDATE)
    except Exception as exc:  # noqa: BLE001
        reject(f"invalid desired state: {type(exc).__name__}: {exc}"[:400], observed)
        return
    current = active["desired"]
    if current is not None and desired.revision <= current.revision:
        reject(f"revision {desired.revision} must be greater than active revision {current.revision}", observed)
        return
    with lock:
        state.update(pending_revision=desired.revision, reload_state="applying")
    hot = (current is not None and engine.alive()
           and desired_runtime.hot_reload_signature(desired) == desired_runtime.hot_reload_signature(current))
    if hot:
        os.replace(CANDIDATE, APPLIED)
        deadline = time.monotonic() + HOT_ACK_S
        while time.monotonic() < deadline:
            st = engine_status()
            if st and st.get("revision") == desired.revision:
                break
            time.sleep(0.25)
        else:
            reject(f"engine did not apply revision {desired.revision} within {HOT_ACK_S:.0f} s", observed)
            return
        kind = "desired_state_hot_reloaded"
        with lock:
            state["hot_count"] += 1
    else:
        previous = APPLIED.read_bytes() if APPLIED.exists() else None
        engine.stop()
        os.replace(CANDIDATE, APPLIED)
        if not engine.start():
            engine.stop()
            if previous is not None:
                APPLIED.write_bytes(previous)
                ok = engine.start()
                log(f"last valid revision {current.revision} restarted: {'ok' if ok else 'FAILED'}")
            reject(f"revision {desired.revision}: engine did not start with it; last valid revision kept", observed)
            return
        kind = "desired_state_applied"
    active["desired"] = desired
    with lock:
        state.update(ready=True, active_revision=desired.revision, active_hash=observed, pending_revision=None,
                     reload_state="idle", latest_error="", last_reload_at=time.time())
        state["applied_count"] += 1
        state.pop("rejected_hash", None)
    event(kind, revision=desired.revision, cameras=len(desired.cameras), engine_pid=state["engine_pid"])


STALE_S = 10.0       # contract-side freshness: no frame for this long -> not healthy (her runtime uses 10 s too)
HEARTBEAT_S = 5.0    # engine status older than this -> heartbeat silence
PRESSURE = 0.95      # achieved below 95 % of the requested fps -> runtime pressure reported (requested fps never changed)
samples = []         # (time, engine status) for rates over the last RATE_S seconds
RATE_S = 10.0


def utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1000):03d}Z" if epoch else None


def sampler():
    """Keeps recent engine status samples so per-camera rates are measured, not guessed."""
    while not stop.is_set():
        st = engine_status()
        if st:
            with lock:
                if not samples or samples[-1][1]["at"] != st["at"]:
                    samples.append((st["at"], st))
                while samples and (samples[-1][0] - samples[0][0] > RATE_S + 2 or samples[0][1].get("pid") != st.get("pid")):
                    samples.pop(0)
        stop.wait(1.0)


def rates():
    """Per camera: decoded fps (frames in, after the 10 fps cap), inference fps (main detector output), events fps
    (frames processed by her V8 code), over the sample window. A counter reset (camera rebuilt) gives no rate."""
    with lock:
        if len(samples) < 2:
            return {}
        (t0, a), (t1, b) = samples[0], samples[-1]
    dt = t1 - t0
    out = {}
    for cid, cb in b.get("cameras", {}).items():
        ca = a.get("cameras", {}).get(cid)
        if ca is None or dt <= 0:
            continue
        r = {}
        for key, name in (("frames_in", "decoded_fps"), ("main_out", "inference_fps"), ("frames_done", "events_fps")):
            d = cb.get(key, 0) - ca.get(key, 0)
            r[name] = round(d / dt, 2) if d >= 0 else None
        out[cid] = r
    return out


def per_camera_state():
    """Contract camera states healthy / degraded / unavailable with reason, last_reliable_at, as_of and retrying
    (runtimeEndpoints.readiness.responseIncludes, managementCompatibility.cameraHealthStates)."""
    st = engine_status() or {}
    now = time.time()
    silent = not engine.alive() or not st or now - st.get("at", 0) > HEARTBEAT_S
    rate = rates()
    out = []
    configured = [(c.camera_id, c.fps) for c in active["desired"].cameras] if active["desired"] else []
    for camera_id, requested in configured:
        c = (st.get("cameras") or {}).get(camera_id)
        if silent:
            state, reason = "unavailable", "heartbeat_silence" if engine.alive() else "worker_not_running"
        elif c is None or not c["seen"] and c["running"]:
            state, reason = "degraded", "worker_starting"
        elif not c["running"]:
            state, reason = "unavailable", "stream_disconnected"
        elif c.get("last_frame_age_s") is None or c["last_frame_age_s"] > STALE_S:
            state, reason = "degraded", "stale_frames"
        else:
            state, reason = "healthy", "none"
        item = {"camera_id": camera_id, "state": state, "reason": reason,
                "last_reliable_at": utc(c.get("last_frame_at")) if c else None, "as_of": utc(now),
                "retrying": state != "healthy", "requested_fps": requested}
        if c:
            item.update(last_frame_age_seconds=c.get("last_frame_age_s"), reconnects=c.get("rebuilds", 0),
                        retry_backoff_seconds=c.get("retry_in_s", 0.0), **rate.get(camera_id, {}))
        out.append(item)
    return out


def capacity(cameras):
    """retention.configuredFPS.capacityResponse: report the resolved aggregate fps and runtime pressure; the requested
    fps is never changed silently."""
    requested = sum(c["requested_fps"] for c in cameras)
    measured = [c for c in cameras if c.get("inference_fps") is not None and c["state"] == "healthy"]
    resolved = round(sum(c["inference_fps"] for c in measured), 1) if measured else None
    below = [c["camera_id"] for c in measured if c["inference_fps"] < PRESSURE * c["requested_fps"]]
    return {"requested_aggregate_fps": requested, "resolved_aggregate_fps": resolved,
            "cameras_below_requested_fps": below,
            "runtime_pressure": None if resolved is None else ("high" if below else "none"),
            "rate_window_seconds": RATE_S}


def metrics_text():
    snap = snapshot()
    cams = per_camera_state()
    cap = capacity(cams)
    lines = ["# TYPE apexfabric_runtime_ready gauge", f"apexfabric_runtime_ready {int(snap['ready'])}",
             "# TYPE apexfabric_runtime_cameras gauge", f"apexfabric_runtime_cameras {len(cams)}",
             "# TYPE apexfabric_runtime_revision gauge", f"apexfabric_runtime_revision {snap['active_revision'] or 0}",
             "# TYPE sentinel_runtime_worker_running gauge", f"sentinel_runtime_worker_running {int(snap['worker_running'])}",
             "# TYPE sentinel_runtime_requested_fps gauge", f"sentinel_runtime_requested_fps {cap['requested_aggregate_fps']}",
             "# TYPE sentinel_runtime_resolved_fps gauge", f"sentinel_runtime_resolved_fps {cap['resolved_aggregate_fps'] or 0}",
             "# TYPE sentinel_runtime_pressure gauge", f"sentinel_runtime_pressure {int(cap['runtime_pressure'] == 'high')}"]
    for name in ("applied_count", "hot_count", "rejected_count", "engine_starts", "unexpected_exits"):
        lines += [f"# TYPE sentinel_desired_state_{name}_total counter", f"sentinel_desired_state_{name}_total {snap[name]}"]
    per = (("sentinel_camera_decoded_fps", "decoded_fps", "gauge"), ("sentinel_camera_inference_fps", "inference_fps", "gauge"),
           ("sentinel_camera_events_processed_fps", "events_fps", "gauge"), ("sentinel_camera_requested_fps", "requested_fps", "gauge"),
           ("sentinel_camera_last_frame_age_seconds", "last_frame_age_seconds", "gauge"),
           ("sentinel_camera_reconnects_total", "reconnects", "counter"))
    for metric, key, kind in per:
        lines.append(f"# TYPE {metric} {kind}")
        lines += [f'{metric}{{camera_id="{c["camera_id"]}"}} {c[key]}' for c in cams if c.get(key) is not None]
    lines.append("# TYPE sentinel_camera_state gauge")
    for c in cams:
        for s in ("healthy", "degraded", "unavailable"):
            lines.append(f'sentinel_camera_state{{camera_id="{c["camera_id"]}",state="{s}"}} {int(c["state"] == s)}')
    return "\n".join(lines) + "\n"


def snapshot():
    with lock:
        snap = {k: v for k, v in state.items() if k not in ("events", "rejected_hash")}
        snap["events"] = list(state["events"][-20:])
    st = engine_status()
    snap["engine_status_age_s"] = round(time.time() - st["at"], 2) if st else None
    snap["worker_running"] = engine.alive()
    snap["ready"] = bool(snap["ready"] and snap["worker_running"] and st is not None and time.time() - st["at"] < 5)
    return snap


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            snap = snapshot()
            snap["per_camera_state"] = per_camera_state()
            snap["capacity"] = capacity(snap["per_camera_state"])
            self._json(snap, 200)
        elif path == "/metrics":
            body = metrics_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/readyz":
            snap = snapshot()
            payload = {"ready": snap["ready"], "worker_running": snap["worker_running"],
                       "active_revision": snap["active_revision"], "pending_revision": snap["pending_revision"],
                       "reload_state": snap["reload_state"], "latest_error": snap["latest_error"],
                       "per_camera_state": per_camera_state()}
            self._json(payload, 200 if snap["ready"] else 503)
        else:
            self._json({"error": "not found"}, 404)

    def _json(self, payload, status):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    host, port = env("APEX_API_HOST", "0.0.0.0"), int(env("APEX_API_PORT", "8080"))
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    log(f"health on http://{host}:{port}/readyz, watching {SOURCE} every {INTERVAL:g} s")
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    end = time.monotonic() + DURATION if DURATION else None
    backoff = 2.0
    while not stop.is_set() and (end is None or time.monotonic() < end):
        if active["desired"] is not None and not engine.alive():  # engine died on its own: same revision again
            with lock:
                state["unexpected_exits"] += 1
                state["ready"] = False
            event("engine_exited", code=engine.proc.returncode if engine.proc else None, restart_in_s=backoff)
            engine.stop()
            stop.wait(backoff)
            backoff = min(60.0, backoff * 2)
            if engine.start():
                backoff = 2.0
                with lock:
                    state["ready"] = state["latest_error"] == ""
            continue
        try_reload()
        stop.wait(INTERVAL)
    log("stopping")
    engine.stop()
    server.shutdown()


if __name__ == "__main__":
    main()
