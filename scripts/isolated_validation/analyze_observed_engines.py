#!/usr/bin/env python3
"""Align node hardware samples to completed case steady monotonic intervals.

Observers and harness run on the SAME remote host. Their monotonic clocks are
comparable; source-host clocks are not used. Node counters are not CV-exclusive.
"""
import argparse
from collections import defaultdict
import gzip
import json
import math
from pathlib import Path


def records(path):
    try:
        with gzip.open(path, 'rt') as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    break
    except (EOFError, OSError):
        # A live mirror can end in an incomplete final compressed member.
        # Keep complete preceding samples; never insert zero utilization.
        return


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return {'samples': 0, 'status': 'unavailable'}
    def percentile(q):
        index = q*(len(values)-1)
        left = int(index)
        right = min(left+1, len(values)-1)
        return values[left]+(values[right]-values[left])*(index-left)
    return {'samples': len(values), 'min': values[0], 'p50': percentile(.5),
            'p95': percentile(.95), 'max': values[-1]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    destination = args.root/'remote-artifacts'
    observation_segments = sorted(p for p in destination.glob('observations*') if p.is_dir())
    output = {'scope': __doc__, 'notes': [
        'GPU engines overlap; do not sum their busy percentages.',
        'Package and GPU watts overlap; do not sum them.',
        'Reported temperature is the maximum CPU core/package sensor per host sample, not unavailable GPU/NPU temperatures.',
        'Samples are approximately one-second intervals; distributions are of samples, not individual inference calls.',
        'GPU samples must fall wholly within the steady interval based on receipt time and tool period; receipt is not a hardware timestamp.',
    ], 'cases': {}}
    intervals = []
    for path in sorted(destination.glob('runs/*/*/raw-*/report.json')):
        report = json.loads(path.read_text())
        start, end = report.get('steady_started_monotonic'), report.get('steady_ended_monotonic')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        intervals.append((path, report.get('passed'), start, end, defaultdict(list), {}))
    sample_counts = {}
    for kind, pattern in [('gpu', 'gpu-*/samples.jsonl.gz'),
                          ('host', 'host-*/host-observations.jsonl.gz'),
                          ('process', 'process-*.jsonl.gz')]:
        sample_counts[kind] = 0
        sources = sorted(source for segment in observation_segments for source in segment.glob(pattern))
        for source in sources:
            for record in records(source):
                sample_counts[kind] += 1
                observed = record.get('received_monotonic', record.get('monotonic'))
                if not isinstance(observed, (int, float)):
                    continue
                for path, passed, start, end, values, camera_counters in intervals:
                    if not start <= observed <= end:
                        continue
                    if kind == 'gpu':
                        item = record['sample']
                        period = item.get('period', {}).get('duration', 1000)/1000
                        if observed-period < start:
                            continue
                        for name, measurement in item.get('engines', {}).items():
                            values['GPU_engine_'+name+'_busy_pct'].append(measurement.get('busy'))
                        for name in ['Package', 'GPU']:
                            values[name+'_W'].append(item.get('power', {}).get(name))
                        values['GPU_frequency_MHz'].append(item.get('frequency', {}).get('actual'))
                    elif kind == 'host':
                        values['host_CPU_pct'].append(record.get('host_cpu_percent'))
                        temperatures = [v['current'] for name, sensors in record.get('temperatures', {}).items()
                                        if name in ['coretemp', 'x86_pkg_temp'] for v in sensors
                                        if isinstance(v.get('current'), (int, float))]
                        if temperatures:
                            values['CPU_sensor_max_C'].append(max(temperatures))
                        values['NPU_node_busy_pct'].append(record.get('accelerators', {}).get('NPU', {}).get('hardware_utilization_pct'))
                        for camera in record.get('health', {}).get('telemetry', {}).get('cameras', []):
                            processing = camera.get('processing', {})
                            gauges = processing.get('gauges', {})
                            if not all(k in gauges for k in ['dls_rate_gate_accepted_frames','dls_rate_gate_dropped_frames']):
                                continue
                            point = {'uptime': camera.get('uptime_seconds'),
                                     'raw': gauges['dls_rate_gate_accepted_frames']+gauges['dls_rate_gate_dropped_frames'],
                                     'epoch': [camera.get('pid'),camera.get('started_at'),camera.get('decoder',{}).get('stream_session_id')]}
                            if not isinstance(point['uptime'], (int,float)):
                                continue
                            item = camera_counters.setdefault(camera['camera_id'], {'first':point,'last':point,'epoch_changed':False})
                            if point['epoch'] != item['first']['epoch'] or point['raw'] < item['last']['raw']:
                                item['epoch_changed'] = True
                            if point['uptime'] > item['last']['uptime']:
                                item['last'] = point
                    else:
                        frequencies = [int(v)/1000 for v in record.get('cpu_frequency_khz', {}).values()]
                        if frequencies:
                            values['CPU_max_reported_frequency_MHz'].append(max(frequencies))
    for path, passed, start, end, values, camera_counters in intervals:
        key = path.parents[1].name+'/'+path.parent.name
        raw_rates = {}
        for camera, item in camera_counters.items():
            duration = item['last']['uptime']-item['first']['uptime']
            valid = duration > 0 and not item['epoch_changed']
            raw_rates[camera] = {'status':'measured' if valid else 'unavailable_epoch_or_duration',
                'raw_decoded_fps':(item['last']['raw']-item['first']['raw'])/duration if valid else None,
                'counter_coverage_seconds':duration,'steady_seconds':end-start,
                'scope':'Primary FPS-gate accepted+dropped counters; actual decoded buffers before FPS selection, during counter coverage only. Does not qualify freshness or throughput.'}
        output['cases'][key] = {'case_passed': passed, 'capacity_qualified': False,
                                'steady_monotonic_range': [start, end],
                                'raw_decode_fps_by_camera':raw_rates,
                                'metrics': {k: distribution(v) for k, v in values.items()}}
    args.output.write_text(json.dumps(output, indent=2)+'\n')
    print(json.dumps({'case_intervals': len(output['cases']), 'observer_samples': sample_counts}))
