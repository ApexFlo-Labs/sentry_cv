#!/usr/bin/env python3
"""Derive stage work from cumulative counters during recorded steady windows.

The counter-coverage interval can be shorter than the steady interval. Values
are invocation wall times, not hardware kernel times or calibrated percentiles.
Parent spans and child stages overlap and must not be summed.
"""
import argparse
import csv
import json
import math
from pathlib import Path

from analyze_observed_engines import records


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def analyze(root):
    destination = root/'remote-artifacts'
    intervals = []
    for path in sorted(destination.glob('runs/*/*/raw-*/report.json')):
        report = json.loads(path.read_text())
        begin, end = report.get('steady_started_monotonic'), report.get('steady_ended_monotonic')
        if finite(begin) and finite(end) and end > begin:
            intervals.append({'case': path.parents[1].name, 'raw': path.parent.name,
                'image_config_id': report.get('image_config_id'), 'probe_passed': report.get('passed'),
                'start': begin, 'end': end, 'cameras': {}})
    sources = sorted(p for segment in destination.glob('observations*') if segment.is_dir()
                     for p in segment.glob('host-*/host-observations.jsonl.gz'))
    for source in sources:
        for record in records(source):
            observed = record.get('monotonic')
            if not finite(observed):
                continue
            matches = [row for row in intervals if row['start'] <= observed <= row['end']]
            if not matches:
                continue
            for camera in record.get('health', {}).get('telemetry', {}).get('cameras', []):
                uptime = camera.get('uptime_seconds')
                if not finite(uptime) or camera.get('fresh') is not True:
                    continue
                cid = camera.get('camera_id')
                epoch = (camera.get('pid'), camera.get('started_at'),
                         camera.get('decoder', {}).get('stream_session_id'))
                frames = camera.get('processing', {}).get('totals', {}).get('inference_frames')
                for row in matches:
                    target = row['cameras'].setdefault(cid, {'epoch': epoch,
                        'epoch_changed': False, 'stages': {}})
                    if epoch != target['epoch']:
                        target['epoch_changed'] = True
                        continue
                    for name, timing in camera.get('use_case_processing', {}).items():
                        count, avg = timing.get('calls_total'), timing.get('lifetime_avg_ms')
                        if not finite(count) or not finite(avg) or count < 0 or avg < 0:
                            continue
                        point = {'uptime': uptime, 'calls': count, 'avg': avg, 'frames': frames}
                        stage = target['stages'].setdefault(name, {'first': point, 'last': point,
                            'measurement_type': timing.get('measurement_type'),
                            'includes_child_stages': timing.get('includes_child_stages'), 'invalid': False})
                        if stage['measurement_type'] != timing.get('measurement_type'):
                            stage['invalid'] = True
                        if uptime > stage['last']['uptime']:
                            if count < stage['last']['calls']:
                                stage['invalid'] = True
                            stage['last'] = point
    output = []
    for row in intervals:
        result = {k: row[k] for k in ['case', 'raw', 'image_config_id', 'probe_passed']}
        result.update(steady_seconds=row['end']-row['start'], cameras={})
        for cid, camera in row['cameras'].items():
            stages = {}
            for name, stage in camera['stages'].items():
                first, last = stage['first'], stage['last']
                duration = last['uptime']-first['uptime']
                calls = last['calls']-first['calls']
                total = last['calls']*last['avg']-first['calls']*first['avg']
                valid = not camera['epoch_changed'] and not stage['invalid'] and duration > 0 and calls >= 0 and total >= 0
                frame_delta = (last['frames']-first['frames']
                    if finite(first['frames']) and finite(last['frames']) else None)
                stages[name] = {'status': 'measured' if valid else 'unavailable_counter_or_epoch',
                    'counter_coverage_seconds': duration, 'calls_in_counter_interval': calls,
                    'calls_per_second': calls/duration if valid else None,
                    'weighted_invocation_wall_avg_ms': total/calls if valid and calls else None,
                    'calls_per_completed_model_frame': calls/frame_delta
                        if valid and finite(frame_delta) and frame_delta > 0 else None,
                    'measurement_type': stage['measurement_type'],
                    'includes_child_stages': stage['includes_child_stages']}
            result['cameras'][cid] = {'epoch_changed': camera['epoch_changed'], 'stages': stages}
        output.append(result)
    return {'scope': __doc__, 'notes': [
        'Uses same-reporter cumulative count and lifetime mean deltas; three-decimal rounding limits precision.',
        'Only fresh snapshots received inside the steady window are used. Counter coverage is explicitly reported.',
        'The resulting mean excludes work before the first usable snapshot; it is not a per-call p95.',
        'Request counts depend on object density and policy; app bindings alone do not specify execution demand.',
        'Source-clock/marked-pixel output latency and hardware utilization require their separate reports.'
    ], 'cases': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.root)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    columns = ['case','camera_id','stage','status','calls_per_second',
        'weighted_invocation_wall_avg_ms','calls_per_completed_model_frame',
        'counter_coverage_seconds','measurement_type','includes_child_stages']
    with args.output.with_suffix('.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for case in result['cases']:
            for cid, camera in case['cameras'].items():
                for stage, values in camera['stages'].items():
                    writer.writerow({k: {'case':case['case'],'camera_id':cid,'stage':stage,**values}.get(k)
                                     for k in columns})
    print(json.dumps({'cases':len(result['cases']), 'output':str(args.output)}))


if __name__ == '__main__':
    main()
