import importlib.util
from pathlib import Path

import pytest


SPEC=importlib.util.spec_from_file_location('motion_analysis',Path(__file__).with_name('analyze_motion_transitions.py'))
helper=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(helper)


@pytest.mark.parametrize('fault',['none','no_wake','no_return_to_quiet','all_active','stale','restart','frozen_clock'])
def test_requires_real_quiet_wake_quiet_with_fresh_ordered_counters(fault):
    pairs=[(0,0),(1,39),(41,39),(42,78)]
    if fault=='no_wake':pairs=[(0,0),(1,39),(2,78),(3,117)]
    if fault=='no_return_to_quiet':pairs=[(0,0),(1,39),(41,39),(81,39)]
    if fault=='all_active':pairs=[(0,0),(40,0),(80,0),(120,0)]
    samples=[]
    for index,(inferred,quiet) in enumerate(pairs):
        camera={'telemetry_fresh':True,'stream_session_id':'s','camera_process_pid':51,
            'counter_uptime_seconds':index*5.,'totals':{'inference_frames':inferred,'quiet_frames':quiet}}
        samples.append({'sample_phase':'steady','per_camera':{'cam':camera}})
    if fault=='stale':samples[1]['per_camera']['cam']['telemetry_fresh']=False
    if fault=='restart':samples[1]['per_camera']['cam']['stream_session_id']='new'
    if fault=='frozen_clock':samples[1]['per_camera']['cam']['counter_uptime_seconds']=0
    assert helper.motion_pattern(samples,'cam',8)['passed']==(fault=='none')
