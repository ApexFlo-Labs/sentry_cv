"""Motion gate as T2 (sentry_cv v8-t2-fix2f; user decision 2026-11-24), for her engine's per-camera branch.

T2's design, same element and same numbers; the per-frame work is in C (cmotion.c, appended to the copy of her
cgates.c), like her other per-frame steps - a Python probe per frame froze the whole engine in a test (2026-11-24):
  gvamotiondetect (VA surface, luma) gives the per-frame motion vote ("motion" regions);
  T2's SurfaceSwitch: quiet -> active when motion is seen in 4 of the last 14 frames (or a detection on a check frame);
  active -> quiet after 6 admitted frames in a row with no detector box and no recent fire/smoke box;
  while quiet, one frame every 5 s still goes to the models (safety sweep);
  quiet frames are dropped BEFORE the shared detector (no inference, tracking, faces, plates or pixels).
The camera's event process learns its quiet-frame count from its shared-memory header (QUIET_SLOT, published once a
second by the engine's watchdog) and runs V8EventPipeline.process_quiet for each (as T2's DlsMotionPolicyGate does).
The fire/smoke branch is split off before the gate and is never gated (as T2). As in T2 the "motion" regions stay on the
frame; the rules ignore that label, the detector and the plate/face steps only use their own classes.
MOTION_GATE=0 turns it off (every frame to the detector, as before).
"""
import ctypes
import os

ENABLED = os.getenv("MOTION_GATE", "1") != "0"
QUIET_SLOT = 100  # int64 slot (16..79 are her frame-request slots) in each camera's shared-memory header: quiet frames so far (engine -> event process)
# mpre -> selector: quiet camera -> gvamotiondetect -> funnel; active camera -> straight to the funnel (pad linked in attach)
ELEMENT = ("identity name=mpre{n} silent=true ! output-selector name=msel{n} pad-negotiation-mode=all ! "
           "gvamotiondetect name=mot{n} pixel-diff-threshold=15 motion-threshold=0.05 block-size=64 confirm-frames=1 "
           "min-persistence=1 max-miss=0 min-rel-area=0.0005 ! funnel name=mfun{n} forward-sticky-events=false ! "
           "identity name=mg{n} silent=true ! ")
_STATS = ("tracking", "passed", "quiet", "motion", "sweeps", "fire_hits", "det_hits", "motion_runs", "motion_skipped")


def element(n):
    return ELEMENT.format(n=n) if ENABLED else ""


def _pad(pipeline, name, direction):
    return ctypes.c_void_p(hash(pipeline.get_by_name(name).get_static_pad(direction)))


def attach(pipeline, cam, cg):
    """C probes for camera cam (its branch elements mg<n>, vd<n>, fdet<n>); a reused idle branch keeps its probes and
    is reset for the new camera. Returns the gate handle (None when disabled)."""
    if not ENABLED:
        return None
    cg.cmotion_new.restype = ctypes.c_void_p
    cg.cmotion_attach.argtypes = [ctypes.c_void_p] * 4
    cg.cmotion_reset.argtypes = [ctypes.c_void_p]
    cg.cmotion_stats.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gate = cam.get("motion")
    if gate is None:
        n = cam["n"]
        gate = cam["motion"] = ctypes.c_void_p(cg.cmotion_new())
        cam["motion_cg"] = cg
        cg.cmotion_attach(gate, _pad(pipeline, f"mg{n}", "sink"), _pad(pipeline, f"vd{n}", "src"),
                          _pad(pipeline, f"fdet{n}", "src"))
        # motion check only while quiet: the selector's second output goes straight to the funnel
        sel, fun = pipeline.get_by_name(f"msel{n}"), pipeline.get_by_name(f"mfun{n}")
        pad_motion = pipeline.get_by_name(f"mot{n}").get_static_pad("sink").get_peer()
        pad_bypass = sel.request_pad_simple("src_%u")
        fun_sink = fun.request_pad_simple("sink_%u")
        if pad_bypass.link(fun_sink) != 0:  # Gst.PadLinkReturn.OK
            raise RuntimeError(f"motion bypass of branch {n} not linked")
        cam["motion_pads"] = (sel, pad_motion, pad_bypass)  # keep the Python objects alive
        cg.cmotion_route.argtypes = [ctypes.c_void_p] * 5
        cg.cmotion_route(gate, _pad(pipeline, f"mpre{n}", "src"), ctypes.c_void_p(hash(sel)),
                         ctypes.c_void_p(hash(pad_motion)), ctypes.c_void_p(hash(pad_bypass)))
    else:
        cg.cmotion_reset(gate)
    cam["motion_quiet_base"] = stats(cam)["quiet"]
    return gate


def stats(cam):
    out = (ctypes.c_long * len(_STATS))()
    cam["motion_cg"].cmotion_stats(cam["motion"], out)
    return dict(zip(_STATS, out))


def publish(cams):
    """Once a second (engine watchdog): each camera's quiet-frame count into its shared-memory header. Counted from
    when the camera got the branch, so a reused branch starts at 0 for the new camera."""
    for c in cams:
        if c.get("motion") is not None and c.get("hdr") is not None:
            c["hdr"][QUIET_SLOT] = stats(c)["quiet"] - c.get("motion_quiet_base", 0)


def summary(cams):
    """One line for the engine report: per camera active/quiet, frames passed/quiet, motion votes, sweeps, hits."""
    parts = []
    for c in cams:
        if c.get("motion") is None:
            continue
        s = stats(c)
        parts.append(f"{c['name']}={'active' if s['tracking'] else 'quiet'} pass={s['passed']} quiet={s['quiet']} "
                     f"motion={s['motion']} sweeps={s['sweeps']} det_hits={s['det_hits']} fire_hits={s['fire_hits']} "
                     f"motion_checked={s['motion_runs']} motion_skipped_active={s['motion_skipped']}")
    return "; ".join(parts)


def quiet_total(cams):
    return sum(stats(c)["quiet"] for c in cams if c.get("motion") is not None)
