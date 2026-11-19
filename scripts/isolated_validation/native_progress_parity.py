"""Actual target SDK pad-counter/lifetime validation; no cameras or inference."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.native_progress import NativeProgressTrace

spec=importlib.util.spec_from_file_location('candidate_native_pad_progress','/checks/native_pad_progress.py')
candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
Gst.init([])
pipeline=Gst.parse_launch('appsrc name=source is-live=true format=time caps=video/x-raw,format=GRAY8,width=4,height=4,framerate=10/1 ! identity name=decoder ! identity name=primarydet ! appsink name=vasink sync=false')
source=pipeline.get_by_name('source');sink=pipeline.get_by_name('vasink')
old=NativeProgressTrace('fixture','session')
pairs=[('decoder','sink'),('decoder','src'),('primarydet','sink'),('primarydet','src'),('vasink','sink')]
stages=[f'{name}.{direction}'for name,direction in pairs]


def attach():
    result=candidate.NativePadProgress('/checks/libsentinel_gst_progress.so')
    for (name,direction),stage in zip(pairs,stages):
        result.attach(pipeline.get_by_name(name).get_static_pad(direction),stage)
    return result


def send(index):
    b=Gst.Buffer.new_allocate(None,16,None);b.pts=index*100000000;b.duration=100000000
    assert source.emit('push-buffer',b)==Gst.FlowReturn.OK
    sample=sink.emit('try-pull-sample',2*Gst.SECOND)
    assert sample is not None and sample.get_buffer().pts==b.pts


native=attach();peer=attach();old.attach(pipeline,Gst)
try:
    assert pipeline.set_state(Gst.State.PLAYING)!=Gst.StateChangeReturn.FAILURE
    for i in range(20):send(i)
    a=old.snapshot()['stages'];b=native.snapshot()
    for stage in stages:
        assert a[stage]['count']==b[stage][0]==20
        assert a[stage]['pts_ns']==b[stage][2]==1900000000
        assert abs(time.monotonic()-b[stage][1])<1
    native.close();native.close()
    for i in range(20,25):send(i)
    assert all(v[0]==20 for v in native.snapshot().values())
    assert all(v[0]==25 for v in peer.snapshot().values())
    assert all(old.snapshot()['stages'][s]['count']==25 for s in stages)
    # Repeatedly attach/close on the SAME live pads. Removing observers must
    # neither drop media nor leave callbacks referring to freed C state.
    for index in range(100):
        n=attach()
        try:
            send(25+index)
            assert all(v[0]==1 and v[2]==(25+index)*100000000 for v in n.snapshot().values())
        finally:n.close()
    assert all(v[0]==125 for v in peer.snapshot().values())
    print(json.dumps({'passed':True,'native_and_trace_counts_exact':True,
        'native_and_trace_pts_exact':True,'peer_native_state_isolated':True,
        'reference_trace_native':bool(getattr(old,'_native',None)),
        'pads':len(stages),'initial_buffers':20,
        'buffers_after_close_without_drop':5,'attach_close_cycles':100,
        'total_buffers':125,'gstreamer_version':Gst.version_string(),
        'scope':'Real target SDK, CPU fixture only; not native recovery, hardware inference or capacity qualification'}))
finally:
    native.close();peer.close();old.close();pipeline.set_state(Gst.State.NULL)
