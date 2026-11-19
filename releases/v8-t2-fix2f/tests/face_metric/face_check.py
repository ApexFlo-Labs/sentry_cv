"""Face success measure check. Runtime face chain exactly as T2 (SCRFD on person ROIs, inference-interval=3, threshold 0.55,
face_align embedding on NPU), with the runtime bridge's counting rule (first buffer and every 3rd = evaluated).
Prints, per evaluated frame, people vs faces matched to a person (same 'face centre inside person box' rule),
and the same on non-evaluated frames.  usage: face_check.py VIDEO FPS"""
import sys, statistics as st, gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from gstgva import VideoFrame
video, FPS = sys.argv[1], int(sys.argv[2])
Gst.init([]); CFG = '/opt/traffic-pilot/config'
P = (f'filesrc location="{video}" ! qtdemux ! h264parse ! vah264dec ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! '
     f'videorate drop-only=true ! video/x-raw(memory:VAMemory),framerate={FPS}/1 ! '
     'gvadetect model=/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml '
     f'model-proc={CFG}/vehicle_yolo26_model_proc.json device=GPU pre-process-backend=va-surface-sharing threshold=0.3 nireq=4 ! '
     'gvatrack tracking-type=zero-term ! '
     'gvadetect model=/models/face/openvino/dlstreamer/scrfd_top1x_u8.xml '
     f'model-proc={CFG}/scrfd_top1_proc.json device=GPU pre-process-backend=va-surface-sharing '
     'inference-region=roi-list object-class=person inference-interval=3 threshold=0.55 nireq=4 ! '
     'gvaclassify model=/models/face/openvino/dlstreamer/face_align_u8.xml device=NPU pre-process-backend=opencv object-class=face nireq=4 ! '
     'appsink name=s sync=false max-buffers=8 drop=false')
p = Gst.parse_launch(P); s = p.get_by_name('s'); p.set_state(Gst.State.PLAYING)
idx = 0; ev, nev = [], []
while True:
    smp = s.emit('try-pull-sample', 10 * Gst.SECOND)
    if smp is None: break
    regs = list(VideoFrame(smp.get_buffer(), caps=smp.get_caps()).regions())
    ppl = [r.rect() for r in regs if r.label() == 'person']; faces = [r.rect() for r in regs if r.label() == 'face']
    def inside(pr, fr): cx, cy = fr.x + fr.w / 2, fr.y + fr.h / 2; return pr.x <= cx <= pr.x + pr.w and pr.y <= cy <= pr.y + pr.h
    owned = sum(1 for fr in faces if any(inside(pr, fr) for pr in ppl))
    big = [pr for pr in ppl if pr.h >= 120]
    (ev if idx % 3 == 0 else nev).append((len(ppl), owned, len(big)))
    idx += 1
p.set_state(Gst.State.NULL)
def summ(rows, name):
    r = [o / n for n, o, _ in rows if n]
    print(f'{name}: frames={len(rows)} people/frame={st.mean(n for n,_,_ in rows):.1f} faces/frame={st.mean(o for _,o,_ in rows):.2f} '
          f'median(faces/people)={st.median(r) if r else None:.3f} mean={st.mean(r) if r else 0:.3f} frames_with_0_faces={sum(1 for _,o,_ in rows if o==0)}')
summ(ev, 'EVALUATED (bridge rule)'); summ(nev, 'NOT evaluated')
