#!/usr/bin/env python3
"""Two-process image path, child side (test): receives GPU frame handles (DMABuf fds + layout) from the engine over a
Unix SOCK_SEQPACKET socket, imports them into GStreamer (appsrc DMABuf -> vapostproc -> vajpegenc) and makes the JPEG on
the GPU's media engine. No raw frame is ever copied to the CPU; only the JPEG bytes come out (counted; first few saved).
Usage: image_proc.py SOCKET_PATH OUT_DIR [SECONDS]"""
import os, socket, struct, sys, time, array
import gi
gi.require_version("Gst", "1.0"); gi.require_version("GstAllocators", "1.0"); gi.require_version("GstVideo", "1.0"); gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstAllocators, GstVideo, GstApp  # noqa: E402

path, out = sys.argv[1], sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 60
os.makedirs(out, exist_ok=True)
Gst.init(None)
MSG = struct.Struct("<qqIIII4I4QII4I4I4Ii")  # must match Msg in cexport.c
alloc = GstAllocators.DmaBufAllocator.new()
pipes = {}
stats = {"frames": 0, "jpegs": 0, "bytes": 0, "saved": 0}


def make_pipe(cam, w, h, modifier):
    caps = f"video/x-raw(memory:DMABuf),format=DMA_DRM,drm-format=NV12:0x{modifier:016x},width={w},height={h},framerate=0/1"
    p = Gst.parse_launch(f"appsrc name=src is-live=true format=time caps=\"{caps}\" ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 "
                         f"! vajpegenc quality=88 ! appsink name=sink emit-signals=true sync=false max-buffers=8 drop=true")
    sink = p.get_by_name("sink")

    def on_sample(s):
        smp = s.emit("pull-sample")
        b = smp.get_buffer()
        stats["jpegs"] += 1
        stats["bytes"] += b.get_size()
        if stats["saved"] < 3:
            ok, mi = b.map(Gst.MapFlags.READ)
            if ok:
                open(os.path.join(out, f"cam{cam}_{stats['saved']}.jpg"), "wb").write(bytes(mi.data))
                b.unmap(mi)
                stats["saved"] += 1
        return Gst.FlowReturn.OK
    sink.connect("new-sample", on_sample)
    p.set_state(Gst.State.PLAYING)
    return p, p.get_by_name("src")


if os.path.exists(path):
    os.remove(path)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
srv.bind(path); srv.listen(32); srv.settimeout(0.2)
conns = []
end = time.time() + seconds
last = time.time()
print(f"image process ready on {path}", flush=True)
while time.time() < end:
    try:
        c, _ = srv.accept(); c.setblocking(False); conns.append(c)
    except socket.timeout:
        pass
    for c in list(conns):
        while True:
            try:
                data, anc, flags, _ = c.recvmsg(MSG.size, socket.CMSG_SPACE(4 * 4))
            except BlockingIOError:
                break
            if not data:
                conns.remove(c); break
            fds = []
            for lvl, typ, cd in anc:
                if lvl == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                    fds.extend(array.array("i", cd[:len(cd) - len(cd) % 4]))
            v = MSG.unpack(data)
            seq, pts, w, h, fourcc, nobj = v[0:6]; sizes = v[6:10]; mods = v[10:14]; drm_format, nplanes = v[14:16]
            obj_idx, offs, pitches, cam = v[16:20], v[20:24], v[24:28], v[28]
            stats["frames"] += 1
            if cam not in pipes:
                pipes[cam] = make_pipe(cam, w, h, mods[0])
            src = pipes[cam][1]
            buf = Gst.Buffer.new()
            for i, fd in enumerate(fds):
                buf.append_memory(GstAllocators.DmaBufAllocator.alloc(alloc, fd, sizes[i]))  # takes ownership of fd
            GstVideo.buffer_add_video_meta_full(buf, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.NV12, w, h, nplanes,
                                                list(offs[:nplanes]) + [0] * (4 - nplanes), list(pitches[:nplanes]) + [0] * (4 - nplanes))
            buf.pts = Gst.CLOCK_TIME_NONE
            src.emit("push-buffer", buf)
    if time.time() - last >= 10:
        last = time.time()
        print(f"{time.strftime('%T')} image process: frames {stats['frames']} jpegs {stats['jpegs']} avg {stats['bytes'] / max(1, stats['jpegs']) / 1024:.0f} KB", flush=True)
print(f"image process done: {stats}", flush=True)
for p, _ in pipes.values():
    p.set_state(Gst.State.NULL)
