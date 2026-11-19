"""Image entrypoint: the V8 solution supervisor (commit b645c04, file unchanged) with three settings for this engine.

1. Per-camera revisions (2026-11-19). When a revision keeps the same deployment, the worker keeps running and
   applies it camera by camera (integrate9 apply_revision, patched): settings live through her
   V8EventPipeline.reconfigure(), fps/source by rebuilding only that camera's source, profiles by restarting only
   that camera's event process, a removed camera stops alone, an added camera gets its own branch in the running
   graph. Only a changed deployment restarts the whole worker (below).
2. Stop the old worker, then start the new one (V8 starts the new one first). Her engine holds every camera connection
   and one shared GPU detector for all cameras: a second engine cannot connect to single-client sources and would
   double GPU/NPU load. The old worker gets WORKER_STOP_SECONDS (default 30) to drain in-flight handoffs.
3. If the new revision's worker does not start, the last valid revision is restarted and the new one is reported
   rejected (contract desiredState.invalid: keep_last_valid_and_report_not_ready). A revision that failed to start is
   not retried on every poll; only a changed file is tried again.
4. A worker that exits on its own (FATAL: stall, repeated camera-process failures, crash) is restarted with the last
   valid revision, backoff 5 s doubling to 300 s (V8's supervisor leaves a dead worker dead until the next revision).
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from traffic_pilot_runtime import solution_image_entrypoint as supervisor

log = logging.getLogger("sentinel_solution_supervisor")
STOP_SECONDS = float(os.getenv("WORKER_STOP_SECONDS", "30"))
WorkerSupervisor = supervisor.WorkerSupervisor
_start = WorkerSupervisor.start_or_replace

def _can_apply_per_camera(self, desired):
    """Same deployment: the running engine applies the revision camera by camera, cameras added and removed included
    (integrate9 apply_revision); the other cameras keep running. A changed deployment restarts the whole worker."""
    active = self.active_desired
    if active is None or not self.state.snapshot()["worker_running"]:
        return False
    return (active.edge_id, active.deployment_id) == (desired.edge_id, desired.deployment_id)


_original_hot_reload = supervisor.DesiredStateReloader._apply_hot_reload


def _apply_per_camera(self, desired, plan):
    _original_hot_reload(self, desired, plan)
    with _APPLY:  # a later worker restart (crash) uses this revision, not the one the worker started with
        sup = self.supervisor
        last = getattr(sup, "_last_good", None)
        if last is not None:
            # A worker restarted after a crash watches cameras.active.json (others watch cameras.generated.json,
            # which V8's adapter already rewrote): replace it whole, so the engine never reads half a file.
            generated = Path(self.args.generated_dir) / "cameras.generated.json"
            active = generated.with_name("cameras.active.json")
            shutil.copyfile(generated, active.with_suffix(".json.tmp"))
            os.replace(active.with_suffix(".json.tmp"), active)
            sup._last_good = (desired, plan, active)
            sup._failed_hash = None
        sup.expected_camera_ids = tuple(c.camera_id for c in desired.cameras)  # startup admission: current cameras
    log.info("revision applied per camera by the running worker",
             extra={"event": "desired_state_applied_per_camera", "revision": desired.revision})


supervisor.DesiredStateReloader._can_hot_reload = _can_apply_per_camera
supervisor.DesiredStateReloader._apply_hot_reload = _apply_per_camera


def _stop_process(proc, stop_file=None):
    if proc is None or proc.poll() is not None:
        return
    if stop_file is not None:
        try:
            stop_file.touch(exist_ok=True)
        except OSError:
            stop_file = None
    if stop_file is None:
        proc.kill()
    try:
        proc.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning("worker did not stop in time; killed", extra={"event": "worker_stop_timeout"})
        proc.kill()
        proc.wait(timeout=5)


_APPLY = threading.RLock()  # start_or_replace calls stop() while holding it


def start_or_replace(self, desired, plan, cameras_file):
    with _APPLY:
        return _start_or_replace(self, desired, plan, cameras_file)


def _start_or_replace(self, desired, plan, cameras_file):
    if getattr(self, "_failed_hash", None) == desired.content_hash and getattr(self, "_last_good", None) is not None:
        raise RuntimeError("this desired-state revision already failed to start; keeping the last valid revision")
    previous = getattr(self, "_last_good", None)
    if self.process is not None:
        log.info("stopping the running worker before the new revision starts",
                 extra={"event": "worker_stopping_for_revision", "revision": desired.revision})
        self.stop()
    try:
        _start(self, desired, plan, cameras_file)
    except Exception:
        self._failed_hash = desired.content_hash
        if previous is not None:
            log.error("new revision did not start; restarting the last valid revision",
                      extra={"event": "worker_restoring_last_valid", "revision": previous[0].revision})
            try:
                _start(self, *previous)
            except Exception:  # noqa: BLE001
                log.exception("last valid revision did not restart", extra={"event": "worker_restore_failed"})
        raise
    active = Path(cameras_file).with_name("cameras.active.json")
    shutil.copyfile(cameras_file, active)
    self._last_good = (desired, plan, active)
    self._failed_hash = None


def _restart_dead_workers(sup, stop_event):
    backoff = 5.0
    while not stop_event.wait(5):
        proc, last = sup.process, getattr(sup, "_last_good", None)
        if proc is None or proc.poll() is None or last is None:
            if proc is not None and proc.poll() is None:
                backoff = 5.0
            continue
        if not _APPLY.acquire(blocking=False):  # a revision change or stop is in progress: its own exit, not a crash
            continue
        try:
            stopping = sup.process is not proc
        finally:
            _APPLY.release()
        if stopping:
            continue
        log.error("camera worker exited on its own; restarting the last valid revision",
                  extra={"event": "worker_exited_restarting", "exit_code": proc.returncode, "revision": last[0].revision,
                         "backoff_seconds": backoff})
        if stop_event.wait(backoff):
            return
        try:
            with _APPLY:
                if sup.process is proc:  # no new revision was applied meanwhile
                    _start(sup, *last)
            backoff = 5.0
        except Exception:  # noqa: BLE001
            log.exception("worker restart failed", extra={"event": "worker_restart_failed"})
            backoff = min(300.0, backoff * 2)


_original_supervisor_init = WorkerSupervisor.__init__


def _init(self, *a, **k):
    _original_supervisor_init(self, *a, **k)
    self._watch_stop = threading.Event()
    threading.Thread(target=_restart_dead_workers, args=(self, self._watch_stop), daemon=True,
                     name="worker-restarter").start()


_original_stop = WorkerSupervisor.stop


def _stop(self):
    with _APPLY:  # shutdown: the restarter re-checks sup.process under the same lock and sees None
        return _original_stop(self)


WorkerSupervisor.__init__ = _init
WorkerSupervisor.stop = _stop
WorkerSupervisor._stop_process = staticmethod(_stop_process)
WorkerSupervisor.start_or_replace = start_or_replace

if __name__ == "__main__":
    raise SystemExit(supervisor.main())
