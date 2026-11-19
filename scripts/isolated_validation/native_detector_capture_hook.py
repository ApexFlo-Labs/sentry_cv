"""Diagnostic hook: bounded primary metadata capture before gvatrack.

No pixels are mapped and no metadata/buffers are changed. Python probe and
file IO are added, so these executions are not capacity measurements.
"""
import json
import math
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.multi_camera_gst import MultiCameraGstPipeline

LABELS = {'bus', 'car', 'minivan', 'motorcycle', 'rickshaw', 'truck',
          'bicycle', 'person', 'others'}
MAXIMUM = 160


def detection_record(buffer, caps):
    import gi
    gi.require_version('GstAnalytics', '1.0')
    from gi.repository import GstAnalytics, GLib
    import gstgva.meta_registry  # Register native tensor metadata types, no pixel map.
    relation = GstAnalytics.buffer_get_analytics_relation_meta(buffer)
    rows = []
    for od in list(relation) if relation is not None else []:
        if type(od) is not GstAnalytics.ODMtd:
            continue
        label = str(GLib.quark_to_string(od.get_obj_type()) or '')
        if label not in LABELS:
            continue
        located, x, y, w, h, _, _ = od.get_oriented_location()
        confident, confidence = od.get_confidence_lvl()
        if not located or not confident:
            raise ValueError('incomplete native primary detection metadata')
        confidence = float(confidence)
        if not math.isfinite(confidence):
            raise ValueError('nonfinite primary confidence')
        rows.append({'label': label, 'confidence': confidence,
                     'bbox': [int(x), int(y), int(x+w), int(y+h)]})
    structure = caps.get_structure(0)
    return {'pts_ns': int(buffer.pts), 'source_width': int(structure.get_value('width')),
            'source_height': int(structure.get_value('height')), 'detections': rows,
            'boundary': 'primarydet.src before gvatrack', 'pixels_mapped': False}


_original_create = MultiCameraGstPipeline.create_branch


def create_branch(self, camera_id, description, *, configure=None):
    def configured(branch_bin):
        if configure is not None:
            configure(branch_bin)
        primary = branch_bin.get_by_name('primarydet')
        if primary is None:
            raise RuntimeError('diagnostic requires named primarydet element')
        Gst = self.Gst
        state = {'count': 0}
        epoch = time.monotonic_ns()
        directory = Path('/run/sentinel-delivery/native-primary')
        directory.mkdir(exist_ok=True)
        name = re.sub('[^A-Za-z0-9_-]', '_', str(camera_id))
        output = directory/(name+'.jsonl')

        def probe(pad, info):
            try:
                value = detection_record(info.get_buffer(), pad.get_current_caps())
                state['count'] += 1
                value.update(camera_id=str(camera_id), pid=os.getpid(),
                             capture_epoch=epoch, ordinal=state['count'])
                with output.open('a') as stream:
                    stream.write(json.dumps(value, allow_nan=False, separators=(',', ':'))+'\n')
                return Gst.PadProbeReturn.REMOVE if state['count'] >= MAXIMUM else Gst.PadProbeReturn.OK
            except Exception as exc:
                with (directory/(name+'.error.json')).open('w') as stream:
                    json.dump({'error_type': type(exc).__name__, 'error': str(exc),
                               'scope': 'Diagnostic metadata reader failed; production buffer untouched'}, stream)
                return Gst.PadProbeReturn.REMOVE
        primary.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, probe)
    return _original_create(self, camera_id, description, configure=configured)


MultiCameraGstPipeline.create_branch = create_branch
