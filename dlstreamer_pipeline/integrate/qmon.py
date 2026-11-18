"""Measurement only: where in the engine do frames pile up? Samples every queue's fill level once per second after
warm-up and counts drops of leaky queues (overrun signal). A queue that is usually FULL means the element right after
it (or something further down that branch) cannot keep up; usually EMPTY means that element keeps up.
Writes OUT every 10 s: per stage (the element after the queue), mean fill %, % of samples full, drops per second."""
import collections
import time

from gi.repository import GLib


def _label(el):
    if el is None:
        return "?"
    f = el.get_factory().get_name() if el.get_factory() else type(el).__name__
    name = el.get_name()
    if f in ("gvadetect", "gvaclassify", "gvainference"):
        m = str(el.get_property("model"))
        for key, lab in (("vehicle", "vehicle+person detector"), ("scrfd", "SCRFD face detector"), ("smoke_fire", "fire detector"),
                         ("face_align", "face_align (old)")):
            if key in m:
                return f"{f} {lab}"
        return f"{f} {m.rsplit('/', 1)[-1]}"
    if f == "identity":
        role = {"vd": "C vehicle dedup", "lm": "C landmarks", "fs": "C face size gate", "hp": "C head pose", "ae": "C align+AdaFace",
                "hpc": "align check"}
        for k, v in sorted(role.items(), key=lambda kv: -len(kv[0])):
            if name.startswith(k) and name[len(k):].isdigit():
                return f"identity ({v})"
    if f == "fakesink":
        for k, v in (("evt", "main JSON out (C reader)"), ("pix", "pixel copy for V8 (C reader)"), ("fevt", "fire JSON out"), ("sec", "secondary end")):
            if name.startswith(k) and name[len(k):].isdigit():
                return f"fakesink ({v})"
    if f == "gvapython":
        try:
            return f"gvapython {el.get_property('class')}"
        except Exception:
            return "gvapython"
    if f == "tee" and name.startswith("s") and name[1:].isdigit():
        return "tee (to secondary)"
    if f == "tee" and name.startswith("et"):
        return "tee (JSON / pixels)"
    return f


def attach(pipeline, warm_s, out):
    queues = []
    it = pipeline.iterate_recurse()
    while True:
        ok, el = it.next()
        if ok != 1:  # Gst.IteratorResult.OK
            break
        if el.get_factory() and el.get_factory().get_name() == "queue":
            queues.append(el)
    info = {}
    for q in queues:
        peer = q.get_static_pad("src").get_peer()
        down = peer.get_parent_element() if peer else None
        lab = _label(down)
        if lab == "?":
            parent = q.get_parent()
            lab = f"queue inside camera source bin ({parent.get_name().rstrip('0123456789') if parent else '?'})"
        if q.get_name().startswith("min"):
            lab = "gvadetect vehicle+person detector (input queue, leaky)"
        elif q.get_name().startswith("fin"):
            lab = "gvadetect fire detector (input queue, leaky)"
        info[q] = {"stage": lab, "max": int(q.get_property("max-size-buffers")), "drops": 0, "leaky": int(q.get_property("leaky"))}
        if info[q]["leaky"]:
            q.connect("overrun", lambda queue: info[queue].__setitem__("drops", info[queue]["drops"] + 1))
    agg = {"t0": None, "n": 0}
    fill, full, cnt = collections.Counter(), collections.Counter(), collections.Counter()
    drops0 = {}

    def sample():
        now = time.monotonic()
        if agg["t0"] is None:
            agg["t0"] = now
            for q in queues:
                drops0[q] = info[q]["drops"]
        agg["n"] += 1
        for q in queues:
            st, mx = info[q]["stage"], max(1, info[q]["max"])
            lvl = int(q.get_property("current-level-buffers"))
            fill[st] += lvl / mx
            full[st] += lvl >= mx
            cnt[st] += 1
        if agg["n"] % 10 == 0:
            span = max(1e-6, now - agg["t0"])
            dps = collections.Counter()
            nq = collections.Counter()
            for q in queues:
                dps[info[q]["stage"]] += info[q]["drops"] - drops0[q]
                nq[info[q]["stage"]] += 1
            rows = sorted(cnt, key=lambda s: -fill[s] / cnt[s])
            with open(out, "w") as f:
                f.write(f"queue fill after warm-up, {span:.0f} s, {len(queues)} queues (stage = the element right after the queue)\n")
                f.write(f"{'mean fill':>9} {'% full':>7} {'drops/s':>8} {'queues':>6}  stage\n")
                for s in rows:
                    f.write(f"{100 * fill[s] / cnt[s]:8.0f}% {100 * full[s] / cnt[s]:6.0f}% {dps[s] / span:8.2f} {nq[s]:6d}  {s}\n")
        return True

    GLib.timeout_add_seconds(int(warm_s), lambda: (GLib.timeout_add_seconds(1, sample), False)[1])
    return len(queues)
