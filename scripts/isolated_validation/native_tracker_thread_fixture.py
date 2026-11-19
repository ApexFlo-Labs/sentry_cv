"""Fixed NV12 pixels/detections through baked zero-term GVA tracking.

Run in separate instances of the SAME image with native OpenCV default versus
OPENCV_FOR_THREADS_NUM=1. This is CPU component parity, not accuracy/capacity
or complete native VA decode/inference/event parity.
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.gva_detection_metadata import detections_from_sample
from gstgva import VideoFrame


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    lib = ctypes.CDLL('/opt/opencv/libopencv_core.so.413')
    threads = getattr(lib, '_ZN2cv13getNumThreadsEv')
    threads.restype = ctypes.c_int
    Gst.init([])
    width, height, total = 640, 360, 240
    caps = Gst.Caps.from_string(
        f'video/x-raw,format=NV12,width={width},height={height},framerate=10/1')
    graph = Gst.parse_launch(
        'appsrc name=input is-live=true format=time ! '
        'gvatrack name=tracker tracking-type=zero-term ! '
        'appsink name=output sync=false async=false max-buffers=1 drop=false')
    source, sink = graph.get_by_name('input'), graph.get_by_name('output')
    source.set_property('caps', caps)
    rows = []
    try:
        assert graph.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        for n in range(total):
            # Textured same-class subjects cross; one disappears, returns, then
            # a fully evaluated empty interval and a new arrival are included.
            specs = [(20+n, 32, 36, 100, 'person', 7, 90, 70, 185),
                     (540-n, 38, 36, 100, 'person', 7, 145, 190, 80),
                     (70+n//2, 210, 100, 54, 'car', 1, 190, 110, 135)]
            if 80 <= n < 87:
                specs = specs[1:]
            if 120 <= n < 126:
                specs = []
            if n >= 180:
                specs.append((440, 180, 32, 110, 'person', 7, 125, 90, 170))
            pixels = np.full((height*3//2, width), 128, np.uint8)
            pixels[:height] = 42
            for x, y, w, h, label, cls, yy, u, v in specs:
                pixels[y:y+h, x:x+w] = yy
                pixels[y:y+h:4, x:x+w:4] = min(255, yy+35)
                uv = pixels[height:]
                xx = (x//2)*2
                uv[y//2:(y+h)//2, xx:((x+w)//2)*2:2] = u
                uv[y//2:(y+h)//2, xx+1:((x+w)//2)*2:2] = v
            raw = pixels.tobytes()
            buf = Gst.Buffer.new_allocate(None, len(raw), None)
            buf.fill(0, raw)
            buf.pts = n*100000000
            buf.duration = 100000000
            frame = VideoFrame(buf, caps=caps)
            for x, y, w, h, label, cls, *_ in specs:
                roi = frame.add_region(x, y, w, h, label, .75)
                roi.detection()['label_id'] = cls
            assert source.emit('push-buffer', buf) == Gst.FlowReturn.OK
            sample = sink.emit('try-pull-sample', 5*Gst.SECOND)
            if sample is None:
                message = graph.get_bus().pop_filtered(Gst.MessageType.ERROR)
                raise RuntimeError(str(message.parse_error()) if message else 'tracker output timeout')
            assert int(sample.get_buffer().pts) == n*100000000
            detections = detections_from_sample(sample, {1: 'car', 7: 'pedestrian'})
            rows.append({'frame': n, 'pts_ns': int(sample.get_buffer().pts),
                         'input_pixel_sha256': hashlib.sha256(raw).hexdigest(),
                         'input_detections': [list(v[:6]) for v in specs],
                         'detections': [vars(d) for d in detections]})
        (a.output/'frames.json').write_text(json.dumps(rows, indent=2)+'\n')
        report = {'passed': True, 'frames': len(rows),
                  'detections': sum(len(r['detections']) for r in rows),
                  'native_opencv_threads': threads(), 'tracker': 'zero-term',
                  'scope': __doc__, 'gstreamer': Gst.version_string()}
        (a.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
    finally:
        graph.set_state(Gst.State.NULL)


if __name__ == '__main__':
    main()
