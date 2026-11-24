"""Contract glue for Chandana's integrate9 engine inside the V8 solution image (no change to her files on disk;
the build applies apply_contract_patch.py to copies).

The V8 supervisor (traffic_pilot_runtime/solution_image_entrypoint.py, commit b645c04, unchanged) starts the camera
worker and talks to it only through files:
  APEXFABRIC_CAMERA_READY_DIR/<camera_id>.ready   worker -> supervisor: camera initialised (startup admission, /readyz)
  APEXFABRIC_WORKER_STOP_FILE                     supervisor -> worker: stop and drain (sent on revision change/stop)
  APEXFABRIC_STATE_ROOT/metrics/*.json            worker -> /healthz, /readyz, /metrics (decoder, supervisor, runtime-camera)
This module provides those pieces with V8's own writers (pipeline.runtime_status, pipeline.runtime_telemetry).
"""
import ctypes
import importlib
import os
import signal
import sys
import time
from pathlib import Path

READY_DIR = os.getenv("APEXFABRIC_CAMERA_READY_DIR")
STOP_FILE = os.getenv("APEXFABRIC_WORKER_STOP_FILE")


def enable_crash_report():
    """A crash (SIGSEGV, SIGABRT, ...) prints every thread's Python stack to stderr (the container log). The shim starts
    the engine with env -i, so PYTHONFAULTHANDLER from the image does not reach it; enabled here instead."""
    import faulthandler
    faulthandler.enable(all_threads=True)


def dump_threads(reason):
    """Before the worker stops itself (stall): where every thread is - Python stacks (faulthandler) and, per thread,
    its name and what the kernel says it waits on (wchan), so a freeze can be traced to the step that holds it."""
    import faulthandler
    print(f"{time.strftime('%T')} THREAD DUMP ({reason})", flush=True)
    try:
        for task in sorted(os.listdir("/proc/self/task"), key=int):
            base = f"/proc/self/task/{task}"
            try:
                comm = open(f"{base}/comm").read().strip()
                wchan = open(f"{base}/wchan").read().strip() or "-"
                state = open(f"{base}/stat").read().split(")")[-1].split()[0]
            except OSError:
                continue
            print(f"  thread {task} {comm:<16} state {state} waits in {wchan}", flush=True)
    except OSError:
        pass
    faulthandler.dump_traceback(all_threads=True)
    sys.stderr.flush()


def die_with_parent():
    """Child processes end when the worker ends, also on SIGKILL from the supervisor (no orphan event processes
    publishing with an old revision, no orphan holding NPU/GPU contexts)."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    if os.getppid() == 1:  # parent already gone before prctl took effect
        os._exit(0)


class _LockedCore:
    """ov.Core whose compile_model calls take turns across all camera processes (file lock in the shared cache dir):
    the first compile writes the cache blob, the others read it; concurrent writers of one blob raced each other."""

    def __init__(self, core, lock_path):
        self._core, self._lock_path = core, lock_path

    def compile_model(self, *a, **k):
        import fcntl
        with open(self._lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._core.compile_model(*a, **k)

    def __getattr__(self, name):
        return getattr(self._core, name)


def child(module, function, *args):
    """multiprocessing target: die_with_parent(), then module.function(*args)."""
    enable_crash_report()
    die_with_parent()
    install_shared_memory()  # same shared-memory backing as the worker's main process (DLS_SHM_DIR)
    if module == "event_worker_shm":  # V8's shared_core caches in /tmp/ov_cache: outside the contract's writable mounts,
        here = os.path.dirname(os.path.abspath(__file__))  # and N processes writing the same blobs race each other
        sys.path.insert(0, os.path.join(here, "v8code"))
        from detectors.backends import openvino_detector
        cache = os.path.normpath(os.path.join(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state"), "..", "ov_cache", "events"))
        os.makedirs(cache, exist_ok=True)
        openvino_detector.shared_core.__defaults__ = (cache,)
        openvino_detector._core = _LockedCore(openvino_detector.shared_core(cache), os.path.join(cache, ".compile.lock"))
    return getattr(importlib.import_module(module), function)(*args)


SHM_RESERVE = 8 * 1024 * 1024
SHM_PER_CAMERA = 83565440 + (16 << 20)  # her per-camera frame/JSON ring (event_worker_shm) + package buffer (pkgshm)


class FileSharedMemory:
    """Drop-in for multiprocessing.shared_memory.SharedMemory backed by a file in DLS_SHM_DIR (mmap, MAP_SHARED):
    same name/size/buf/close/unlink behaviour, so her code and her C reader (pointer into buf) work unchanged."""

    def __init__(self, name=None, create=False, size=0):
        import mmap
        import secrets
        self._name = name or f"psm_{secrets.token_hex(8)}"
        self._path = os.path.join(os.environ["DLS_SHM_DIR"], self._name)
        if create:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.ftruncate(fd, size)
        else:
            fd = os.open(self._path, os.O_RDWR)
            size = os.fstat(fd).st_size
        try:
            self._mmap = mmap.mmap(fd, size)
        finally:
            os.close(fd)
        self._size = size
        self._buf = memoryview(self._mmap)

    name = property(lambda self: self._name)
    size = property(lambda self: self._size)
    buf = property(lambda self: self._buf)

    def close(self):
        if self._buf is not None:
            self._buf.release()  # BufferError while a numpy view exists, as the stdlib class
            self._buf = None
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None

    def unlink(self):
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def install_shared_memory(cameras_file=None):
    """As V8 (shared_vehicle/scene_inference): /dev/shm when it holds every camera's blocks + 8 MiB reserve, else files
    in /tmp/sentinel/shm (contract writable mount); SENTINEL_SHARED_MEMORY_DIR selects a directory explicitly.
    Decided once by the worker's main process (DLS_SHM_DIR is inherited by its children)."""
    import multiprocessing
    import multiprocessing.shared_memory as stdlib_shm
    if multiprocessing.parent_process() is None and "DLS_SHM_DIR" not in os.environ:
        import json
        cameras = len(json.load(open(cameras_file))["cameras"]) if cameras_file else 1
        required = cameras * SHM_PER_CAMERA + SHM_RESERVE
        chosen = os.getenv("SENTINEL_SHARED_MEMORY_DIR")
        if not chosen:
            try:
                st = os.statvfs("/dev/shm")
                available = st.f_bavail * st.f_frsize
            except OSError:
                available = 0
            chosen = "/dev/shm" if required <= available else "/tmp/sentinel/shm"
        os.environ["DLS_SHM_DIR"] = chosen
        if chosen != "/dev/shm":
            os.makedirs(chosen, exist_ok=True)
            for old in os.listdir(chosen):  # previous worker's blocks (it is stopped before this one starts)
                try:
                    os.unlink(os.path.join(chosen, old))
                except OSError:
                    pass
        print(f"{time.strftime('%T')} shared memory: {chosen} ({cameras} cameras need {required / 1e6:.0f} MB reserved; "
              f"/dev/shm is used only when it holds all of it)", flush=True)
    if os.environ.get("DLS_SHM_DIR", "/dev/shm") != "/dev/shm":
        stdlib_shm.SharedMemory = FileSharedMemory


def camera_source_setup(_urisourcebin, source):
    """Every camera source, before it connects.
    RTSP: only the camera's video tracks are set up, over TCP. Cameras that also send audio or ONVIF metadata otherwise
    give decodebin3 extra outputs (site report 2026-11-19: "not-linked" on two cameras that FFmpeg read fine; a data
    track listed first was even decoded instead of the video in our test), and UDP is often blocked between networks.
    TLS without CA matching (user decision 2026-11-18): rtsps:// and https:// sources accept any certificate."""
    names = {p.name for p in source.list_properties()}
    if "protocols" in names and source.get_factory().get_name() == "rtspsrc":
        from gi.repository import Gst
        Gst.util_set_object_arg(source, "protocols", "tcp")
        source.connect("select-stream", _video_track_only)
    if "tls-validation-flags" in names:  # rtspsrc (rtsps://)
        from gi.repository import Gio
        source.set_property("tls-validation-flags", Gio.TlsCertificateFlags(0))
    if "ssl-strict" in names:  # souphttpsrc (https://)
        source.set_property("ssl-strict", False)


def _video_track_only(_rtspsrc, _num, caps):
    return caps.get_structure(0).get_string("media") == "video"


def mark_ready(camera_id):
    if READY_DIR:
        Path(READY_DIR, f"{camera_id}.ready").touch()


def unmark_ready(camera_id):
    """Camera removed from the deployment (live): its startup-admission file goes too."""
    if READY_DIR:
        try:
            Path(READY_DIR, f"{camera_id}.ready").unlink()
        except OSError:
            pass


def metric_name(camera_id):
    """File-name form of a camera id in metrics/ (V8 runtime_status.safe_camera_name / runtime_telemetry._safe_name)."""
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(camera_id))[:160] or "camera"


class ConnReply:
    """Queue-like end of a one-way pipe (put / get / get_nowait), for the model-server replies of a camera added while
    the worker runs: a multiprocessing.Queue cannot be handed to an already running process, a Connection can."""

    def __init__(self, conn):
        import threading
        self.conn, self._lock = conn, threading.Lock()

    def put(self, item):
        with self._lock:  # replies come from OpenVINO callback threads
            self.conn.send(item)

    def get(self, timeout=None):
        import queue
        if not self.conn.poll(timeout):
            raise queue.Empty
        return self.conn.recv()

    def get_nowait(self):
        return self.get(0)

    def __getstate__(self):
        return {"conn": self.conn}

    def __setstate__(self, state):
        import threading
        self.conn, self._lock = state["conn"], threading.Lock()


def stop_requested():
    return bool(STOP_FILE) and os.path.exists(STOP_FILE)


class CameraStatus:
    """Per camera, in its event process: V8's decoder / supervisor status files and runtime telemetry, fed from
    the engine's counters (shared-memory header) and the camera_status messages her code already receives."""

    def __init__(self, name, fps, bindings, header):
        from pipeline.runtime_status import DecoderRuntimeStatus, write_supervisor_status
        from pipeline.runtime_telemetry import CameraTelemetry
        devices = {"decode": "GPU/VAAPI-DLStreamer", "vehicle_person_detection": "GPU/DLStreamer-VA",
                   "object_tracking": "CPU/gvatrack", "face_detection": "GPU", "face_embedding": os.getenv("ADA_DEVICE", "NPU"),
                   "face_pose": os.getenv("POSE_DEVICE", "NPU"), "fire_smoke_detection": os.getenv("FIRE_DEVICE") or "GPU",
                   "anpr_plate_detection": "NPU", "anpr_ocr": "NPU",
                   "body_embedding": os.getenv("MODEL_SERVER_DEVICE", "NPU"), "gait_embedding": os.getenv("MODEL_SERVER_DEVICE", "NPU"),
                   "scene_embedding": os.getenv("MODEL_SERVER_DEVICE", "NPU")}
        self.telemetry = CameraTelemetry(name, target_fps=fps, devices=devices,
                                         use_case_catalog={str(b["use_case_id"]): str(b["app"]) for b in bindings})
        self.decoder = DecoderRuntimeStatus(name, "pending")  # real session set by attach() once V8EventPipeline exists
        write_supervisor_status(name, state="running", camera_process_pid=os.getpid(), retry_reason=None,
                                retry_backoff_seconds=0.0)
        self.fps, self.H, self.state = float(fps), header, "starting"
        self.t_last, self.in_last, self.out_last = time.monotonic(), int(header[3]), int(header[0])
        self.quiet_total = self.quiet_last = 0

    def attach(self, session_id):
        self.decoder.set_session(session_id)

    def camera_status(self, state, reason, session_id):
        self.decoder.set_session(session_id)
        if state == "unavailable":
            self.decoder.update(state="unavailable", reason=reason, retry_reason=reason, retry_backoff_seconds=1.0)
        elif state == "healthy" and self.state == "unavailable":
            self.decoder.increment("reconnects")
            self.decoder.increment("decoder_starts")
        self.state = state

    def frame(self):
        self.decoder.frame()
        self.telemetry.frame(inferred=True)

    def skipped(self, key):
        self.telemetry.count(key)

    def quiet(self):
        """A frame the motion gate kept from the models (nothing moved): decoded and seen, not inferred (as T2)."""
        self.decoder.frame()
        self.telemetry.frame(inferred=False)
        self.quiet_total += 1

    def tick(self):
        """Every 2 s: requested vs. delivered fps, frames the engine dropped (leaky queues) - reported, never hidden.
        Motion-gate quiet frames count as delivered (handled, no models needed), never as dropped."""
        now = time.monotonic()
        dt = now - self.t_last
        if dt < 2.0:
            return
        frames_in, frames_out = int(self.H[3]), int(self.H[0])
        d_in, d_out = frames_in - self.in_last, frames_out - self.out_last
        d_quiet = self.quiet_total - self.quiet_last
        self.t_last, self.in_last, self.out_last, self.quiet_last = now, frames_in, frames_out, self.quiet_total
        self.telemetry.set_gauge("requested_fps", self.fps)
        self.telemetry.set_gauge("engine_input_fps", d_in / dt)
        self.telemetry.set_gauge("engine_output_fps", d_out / dt)
        self.telemetry.set_gauge("motion_quiet_fps", d_quiet / dt)
        self.telemetry.set_gauge("engine_dropped_fps", max(0, d_in - d_out - d_quiet) / dt)
        self.telemetry.set_gauge("delivered_fraction_of_requested", ((d_out + d_quiet) / dt) / self.fps if self.fps else 0.0)
        if d_in > d_out + d_quiet:
            self.telemetry.count("engine_dropped_frames", d_in - d_out - d_quiet)
