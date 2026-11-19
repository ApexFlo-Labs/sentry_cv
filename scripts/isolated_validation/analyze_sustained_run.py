#!/usr/bin/env python3
"""Offline sustained-run trends; never converts a short/failed probe into PASS.

Container memory includes allocations/caches charged to its cgroup. A fitted
memory slope is diagnostic, not proof of a leak or proof that none exists.
Rolling health FPS is plotted separately from counter-derived completed work.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def distribution(values):
    values = sorted(v for v in values if finite(v))
    if not values:
        return None
    def quantile(q):
        pos = (len(values)-1)*q
        left = int(pos)
        right = min(left+1, len(values)-1)
        return values[left] + (pos-left)*(values[right]-values[left])
    return {'samples':len(values), 'min':values[0], 'p50':quantile(.5),
            'p95':quantile(.95), 'max':values[-1]}


def trend(points):
    points = [(x,y) for x,y in points if finite(x) and finite(y)]
    if len(points) < 3 or points[-1][0] <= points[0][0]:
        return None
    xs, ys = zip(*points)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x-mx)**2 for x in xs)
    if not denom:
        return None
    slope = sum((x-mx)*(y-my) for x,y in points)/denom
    return {'samples':len(points), 'duration_seconds':xs[-1]-xs[0],
            'slope_MiB_per_minute':slope*60,
            'fitted_change_MiB':slope*(xs[-1]-xs[0]),
            'scope':'Linear diagnostic of cgroup memory samples, not a leak verdict'}


def analyze(report):
    start, end = report['steady_started_monotonic'], report['steady_ended_monotonic']
    samples = [v for v in report['samples'] if v.get('sample_phase')=='steady'
               and finite(v.get('sample_monotonic_seconds'))
               and start <= v['sample_monotonic_seconds'] <= end]
    points = [(v['sample_monotonic_seconds']-start,v.get('container_memory_mb')) for v in samples]
    tail = [p for p in points if p[0] >= max(0,end-start-1800)]
    first = [y for x,y in points if x <= 600 and finite(y)]
    last = [y for x,y in points if x >= end-start-600 and finite(y)]
    cameras = {}
    for sample in samples:
        for cid,camera in sample.get('per_camera',{}).items():
            row = cameras.setdefault(cid, {'fresh_false_samples':0,
                 'peak_last_frame_age_seconds':None, 'peak_pending_admissions':0,
                 'worker_pids':set(), 'stream_sessions':set()})
            row['fresh_false_samples'] += camera.get('telemetry_fresh') is False
            age = camera.get('last_frame_age_seconds')
            if finite(age):
                row['peak_last_frame_age_seconds'] = max(age,row['peak_last_frame_age_seconds'] or 0)
            row['peak_pending_admissions'] = max(row['peak_pending_admissions'],camera.get('pending_admissions') or 0)
            if camera.get('camera_process_pid') is not None:
                row['worker_pids'].add(camera['camera_process_pid'])
            if camera.get('stream_session_id'):
                row['stream_sessions'].add(camera['stream_session_id'])
    for cid, row in cameras.items():
        row['worker_pids'] = sorted(row['worker_pids'])
        row['stream_sessions'] = sorted(row['stream_sessions'])
        row['completed_work'] = report.get('steady_completed_work',{}).get('per_camera',{}).get(cid)
    return {'utc':datetime.now(timezone.utc).isoformat(),
            'image_config_id':report['image_config_id'],
            'probe_passed':report['passed'], 'capacity_qualified':False,
            'steady_seconds':end-start, 'full_hour_observed':end-start>=3600,
            'steady_samples':len(samples),
            'memory_MiB':distribution([p[1] for p in points]),
            'memory_trend_whole_steady':trend(points),
            'memory_trend_last_up_to_30_minutes':trend(tail),
            'first_ten_minute_memory_median_MiB':statistics.median(first) if first else None,
            'last_ten_minute_memory_median_MiB':statistics.median(last) if last else None,
            'container_CPU_cores':distribution([v.get('container_cpu_cores') for v in samples]),
            'unready_samples':sum(v.get('ready') is not True for v in samples),
            'cameras':cameras,
            'scene_coverage':report.get('steady_output_coverage'),
            'source_utc_sanity':report.get('source_utc_sanity'),
            'fatal_lines':report.get('fatal_lines'),
            'notes':[__doc__, 'A source-clock timestamp or rolling FPS alone does not qualify source-pixel freshness.',
                     'Temperature/GPU-engine/NPU hardware metrics are in the independently aligned hardware analysis.',
                     'Passing a replay is limited to that resolution, content, role configuration and power envelope.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=analyze(json.loads(args.report.read_text()))
    result['source_report']=str(args.report.resolve())
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['probe_passed','steady_seconds','full_hour_observed','steady_samples','memory_MiB']}))
