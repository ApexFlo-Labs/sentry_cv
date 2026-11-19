import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('changed_epoch', [False, True])
def test_stage_mean_uses_cumulative_work_and_rejects_worker_changes(tmp_path, changed_epoch):
    root = tmp_path/'remote-artifacts'
    case = root/'runs/example/case/raw-example'
    case.mkdir(parents=True)
    (case/'report.json').write_text(json.dumps({'passed': True,
        'steady_started_monotonic': 100, 'steady_ended_monotonic': 120}))
    for folder, received, uptime, calls, mean, frames, pid in [
        ('observations',101,20,10,5,100,52),
        ('observations-renewed',111,30,30,6,110,99 if changed_epoch else 52)]:
        source = root/folder/'host-1/host-observations.jsonl.gz'
        source.parent.mkdir(parents=True)
        with gzip.open(source,'wt') as stream:
            stream.write(json.dumps({'monotonic':received,'health':{'telemetry':{'cameras':[{
                'camera_id':'a','pid':pid,'started_at':'start','fresh':True,'uptime_seconds':uptime,
                'decoder':{'stream_session_id':'session'},
                'processing':{'totals':{'inference_frames':frames}},
                'use_case_processing':{'body.model':{'calls_total':calls,'lifetime_avg_ms':mean,
                    'measurement_type':'model_invocation_wall_time','includes_child_stages':False}}
            }]}}})+'\n')
    output=tmp_path/'stages.json'
    subprocess.run([sys.executable,str(Path(__file__).with_name('analyze_stage_work.py')),
        str(tmp_path),'--output',str(output)],check=True,capture_output=True)
    camera=json.loads(output.read_text())['cases'][0]['cameras']['a']
    assert camera['epoch_changed']==changed_epoch
    stage=camera['stages']['body.model']
    if changed_epoch:
        assert stage['weighted_invocation_wall_avg_ms'] is None
        assert stage['status']=='unavailable_counter_or_epoch'
    else:
        assert stage['calls_per_second']==2
        assert stage['weighted_invocation_wall_avg_ms']==6.5
        assert stage['calls_per_completed_model_frame']==2
        assert stage['counter_coverage_seconds']==10
        assert stage['measurement_type']=='model_invocation_wall_time'
