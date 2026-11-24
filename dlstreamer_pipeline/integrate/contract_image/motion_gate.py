"""Motion gate as T2 (sentry_cv v8-t2-fix2f; user decision 2026-11-24), for her engine's per-camera branch.

T2's design, same element and same numbers:
  gvamotiondetect (VA surface, luma) gives the per-frame motion vote ("motion" regions);
  Switch = T2's SurfaceSwitch: quiet -> active when motion is seen in 4 of the last 14 frames (or a detection on a
  check frame); active -> quiet after 6 frames in a row where the detector found nothing and fire/smoke saw nothing;
  while quiet, one frame every 5 s still goes to the models (safety sweep);
  quiet frames are dropped BEFORE the shared detector (no inference, tracking, faces, plates or pixels) and are reported
  to the camera's rules as quiet frames (V8EventPipeline.process_quiet), as T2's DlsMotionPolicyGate does.
The fire/smoke branch is split off before the gate and is never gated (as T2). As in T2 the "motion" regions stay on
the frame (DL Streamer's Python API cannot remove them); the rules ignore that label, the detector and the plate/face
steps only use their own classes.
MOTION_GATE=0 turns it off (every frame to the detector, as before).
"""
import collections
import os
import threading
import time

ENABLED = os.getenv("MOTION_GATE", "1") != "0"
ELEMENT = ("gvamotiondetect name=mot{n} pixel-diff-threshold=15 motion-threshold=0.05 block-size=64 confirm-frames=1 "
           "min-persistence=1 max-miss=0 min-rel-area=0.0005 ! identity name=mg{n} silent=true ! ")


def element(n):
    return ELEMENT.format(n=n) if ENABLED else ""


class Switch:
    """T2 surface_stream.SurfaceSwitch (confirm_win 14, confirm_need 4, release_misses 6, sweep 5 s), unchanged logic."""

    def __init__(self, confirm_win=14, confirm_need=4, release_misses=6, sweep_seconds=5.0, start_active=True):
        self.hist = collections.deque(maxlen=confirm_win)
        self.confirm_need, self.release_misses, self.sweep_seconds = confirm_need, release_misses, sweep_seconds
        self.start_active = bool(start_active)
        self.reset()

    def reset(self):
        self.hist.clear()
        self.tracking = self.start_active
        self.miss = self.quiet_frames = self.switches = self.sweeps = self.sweep_hits = 0
        self.last_sweep_at = None

    def update(self, candidate, fps, timestamp=None):
        if timestamp is not None and self.last_sweep_at is None:
            self.last_sweep_at = timestamp
        if not self.tracking:
            self.hist.append(bool(candidate))
            if sum(self.hist) >= self.confirm_need:
                self.tracking, self.miss, self.quiet_frames = True, 0, 0
                self.switches += 1
                return "TRACKING", True
            self.quiet_frames += 1
            sweep_due = (timestamp - self.last_sweep_at >= self.sweep_seconds if timestamp is not None else
                         self.quiet_frames >= max(1, int(round(self.sweep_seconds * fps))))
            if sweep_due:
                self.last_sweep_at, self.quiet_frames = timestamp, 0
                self.sweeps += 1
                return "MOTION", True
            return "MOTION", False
        self.last_sweep_at = timestamp
        return "TRACKING", True

    def observe_models(self, vehicle_hit, smoke_hit, fully_evaluated=True):
        if vehicle_hit or smoke_hit:
            return self.observe(True)
        if not fully_evaluated:
            return False
        return self.observe(False)

    def observe(self, detected):
        if not self.tracking:
            if detected:
                self.sweep_hits += 1
                self.tracking, self.miss, self.quiet_frames = True, 0, 0
                self.switches += 1
            return False
        if detected:
            self.miss = 0
            return False
        self.miss += 1
        if self.miss >= self.release_misses:
            self.tracking, self.miss, self.quiet_frames = False, 0, 0
            self.hist.clear()
            return True
        return False


class Gate:
    """One camera: the gate probe (before the detector), the detector-result probe and the fire-result probe."""

    def __init__(self, fps, on_quiet):
        self.fps, self.on_quiet = float(fps), on_quiet
        self.switch = Switch()
        self.lock = threading.Lock()
        self.passed = self.quiet = self.motion_frames = 0
        self.fire_at, self.fire_hit = 0.0, False

    def reset(self, fps=None):
        with self.lock:
            self.switch.reset()
            if fps:
                self.fps = float(fps)

    def gate_probe(self, pad, info):
        from gi.repository import Gst
        from gstgva import VideoFrame
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        motion = [r for r in VideoFrame(buffer, caps=pad.get_current_caps()).regions() if r.label() == "motion"]
        pts = int(buffer.pts)
        with self.lock:
            self.motion_frames += bool(motion)
            _state, requested = self.switch.update(bool(motion), self.fps, pts / 1e9 if pts >= 0 else None)
            if requested:
                self.passed += 1
                return Gst.PadProbeReturn.OK
            self.quiet += 1
        self.on_quiet(pts)
        return Gst.PadProbeReturn.DROP

    def detections_probe(self, pad, info):
        """After the detector (vd): did it find anything on this admitted frame?"""
        from gi.repository import Gst
        from gstgva import VideoFrame
        buffer = info.get_buffer()
        if buffer is not None:
            hit = any(r.label() != "motion" for r in VideoFrame(buffer, caps=pad.get_current_caps()).regions())
            with self.lock:
                fire_recent = time.monotonic() - self.fire_at <= 1.0
                self.switch.observe_models(hit, fire_recent and self.fire_hit, fully_evaluated=fire_recent)
        return Gst.PadProbeReturn.OK

    def fire_probe(self, pad, info):
        """After the fire/smoke detector (2 frames/s, never gated): a fire/smoke box keeps or makes the camera active."""
        from gi.repository import Gst
        from gstgva import VideoFrame
        buffer = info.get_buffer()
        if buffer is not None:
            hit = bool(list(VideoFrame(buffer, caps=pad.get_current_caps()).regions()))
            with self.lock:
                self.fire_at, self.fire_hit = time.monotonic(), hit
                if hit:
                    self.switch.observe_models(False, True, fully_evaluated=True)
        return Gst.PadProbeReturn.OK


def attach(pipeline, cam, send):
    """Probes for camera cam (its branch elements mg{n}, vd{n}, fdet{n}); returns the Gate (None when disabled)."""
    if not ENABLED:
        return None
    from gi.repository import Gst
    n = cam["n"]
    gate = cam.get("motion")
    if gate is None:
        gate = cam["motion"] = Gate(cam["fps"], lambda pts, c=cam: send(c, ("quiet", pts)))
        pipeline.get_by_name(f"mg{n}").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, gate.gate_probe)
        pipeline.get_by_name(f"vd{n}").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, gate.detections_probe)
        pipeline.get_by_name(f"fdet{n}").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, gate.fire_probe)
    else:  # a reused idle branch: same probes, new camera
        gate.on_quiet = lambda pts, c=cam: send(c, ("quiet", pts))
        gate.reset(cam["fps"])
    return gate


def quiet_total(cams):
    return sum(c["motion"].quiet for c in cams if c.get("motion") is not None)
