"""Actual baked SDK metadata parity and timing; CPU-only, no model claims."""
import importlib.util
import json
import os
import sys
import time
import gi
gi.require_version('Gst','1.0')
gi.require_version('GstAnalytics','1.0')
from gi.repository import Gst,GstAnalytics,GLib
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.gva_detection_metadata import detections_from_sample as reference
if os.getenv('SENTINEL_NATIVE_PARITY_BAKED') == '1':
    from pipeline import gva_native_metadata as candidate
else:
    spec=importlib.util.spec_from_file_location('native_candidate','/checks/gva_native_metadata.py')
    candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
Gst.init([])
classes={0:'bus',1:'pedestrian'}


def sample(objects,missing=False):
    buffer=Gst.Buffer.new_allocate(None,16,None)
    relation=GstAnalytics.buffer_add_analytics_relation_meta(buffer)
    for i in range(objects):
        ok,od=relation.add_od_mtd(GLib.quark_from_string('bus'if i%2==0 else'person'),
            i,i+1,15,26,.812345)
        assert ok
        if not missing:
            ok,track=relation.add_tracking_mtd(1000+i,0);assert ok
            assert relation.set_relation(GstAnalytics.RelTypes.RELATE_TO,od.id,track.id)
    # Unrecognized classes do not become primary detections or require a track.
    ok,_=relation.add_od_mtd(GLib.quark_from_string('face'),0,0,2,2,.9);assert ok
    return Gst.Sample.new(buffer,None,None,None)


counts=0;compared=0;timings={}
for number in [0,1,9,50,300]:
    value=sample(number)
    for _ in range(3):
        a=reference(value,classes);b=candidate.detections_from_sample(value,classes)
        assert [vars(x)for x in a]==[vars(x)for x in b]
        compared+=len(a);counts+=1
    for name,reader in [('reference',reference),('native',candidate.detections_from_sample)]:
        start=time.perf_counter()
        for _ in range(50):reader(value,classes)
        timings.setdefault(str(number),{})[name+'_seconds']=(time.perf_counter()-start)/50

value=sample(1,missing=True)
for reader in [reference,candidate.detections_from_sample]:
    try:reader(value,classes)
    except RuntimeError:pass
    else:raise AssertionError('missing tracking metadata must not be silently accepted')

# Preserve the existing first-related tracking rule, including tracking ID zero.
buffer=Gst.Buffer.new_allocate(None,16,None);relation=GstAnalytics.buffer_add_analytics_relation_meta(buffer)
_,od=relation.add_od_mtd(GLib.quark_from_string('person'),-2,3,11,13,.75)
_,first=relation.add_tracking_mtd(0,0);_,second=relation.add_tracking_mtd(29,0)
relation.set_relation(GstAnalytics.RelTypes.RELATE_TO,od.id,second.id)
relation.set_relation(GstAnalytics.RelTypes.CONTAIN,od.id,first.id)
value=Gst.Sample.new(buffer,None,None,None)
a=reference(value,classes);b=candidate.detections_from_sample(value,classes)
assert [vars(x)for x in a]==[vars(x)for x in b]and b[0].metadata['track_id']==0
empty=Gst.Sample.new(None,None,None,None)
try:candidate.detections_from_sample(empty,classes)
except RuntimeError:pass
else:raise AssertionError('bufferless sample must fail before native pointer access')

print(json.dumps(dict(passed=True,native_samples=counts+1,exact_detections=compared+1,
    missing_track_rejected=True,unknown_class_ignored=True,first_related_track_unchanged=True,
    timings_seconds_per_call=timings,gstreamer=Gst.version_string(),
    scope='Real SDK CPU metadata component parity, not native detector/end-to-end/capacity qualification')))
