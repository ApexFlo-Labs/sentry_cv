"""Actual baked SDK CPU fixture: metadata correctness and every buffer preserved."""
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstAnalytics', '1.0')
from gi.repository import Gst, GstAnalytics, GLib
Gst.init([])
import native_detector_capture_hook
from pipeline.multi_camera_gst import MultiCameraGstPipeline

coordinator = MultiCameraGstPipeline(['fixture'], startup_camera_count=1)
configured = []
branch = coordinator.create_branch('fixture',
    'appsrc name=input is-live=true format=time '
    'caps="video/x-raw,format=BGR,width=64,height=36,framerate=10/1" ! '
    'identity name=primarydet ! fakesink name=output sync=false async=false signal-handoffs=true',
    configure=lambda _: configured.append(True))
completed = threading.Event()
count = [0]
def received(*_):
    count[0] += 1
    if count[0] == 170:
        completed.set()
branch.get_by_name('output').connect('handoff', received)
try:
    branch.set_state(Gst.State.PLAYING)
    for n in range(170):
        buffer = Gst.Buffer.new_allocate(None, 64*36*3, None)
        buffer.pts = n*100000000
        buffer.dts = buffer.pts
        relation = GstAnalytics.buffer_add_analytics_relation_meta(buffer)
        relation.add_od_mtd(GLib.quark_from_string('person'), 3, 4, 12, 22, .75)
        relation.add_od_mtd(GLib.quark_from_string('others'), 21, 1, 7, 7, .625)
        relation.add_od_mtd(GLib.quark_from_string('motion'), 1, 1, 4, 4, .9)
        assert branch.get_by_name('input').emit('push-buffer', buffer) == Gst.FlowReturn.OK
    assert completed.wait(10), count
    rows = [json.loads(x) for x in Path('/run/sentinel-delivery/native-primary/fixture.jsonl').read_text().splitlines()]
    assert configured == [True] and count[0] == 170 and len(rows) == 160
    assert [x['pts_ns'] for x in rows] == [n*100000000 for n in range(160)]
    assert rows[0]['detections'] == [
        {'label': 'person', 'confidence': .75, 'bbox': [3, 4, 15, 26]},
        {'label': 'others', 'confidence': .625, 'bbox': [21, 1, 28, 8]}]
    assert all(not x['pixels_mapped'] for x in rows)
    print(json.dumps({'passed': True, 'input_buffers': 170, 'sink_buffers': count[0],
                      'bounded_raw_frames': len(rows), 'tracking_metadata_required': False,
                      'scope': 'Real SDK CPU metadata/flow fixture, no model or capacity evidence'}))
finally:
    coordinator.close()
