#!/usr/bin/env python3
"""Summarize mirrored, immutable case reports without granting qualification.

This reads artifacts only. It neither runs workloads nor changes their verdicts.
Resource windows and source-clock latency are explicitly separate from independent
source-pixel latency. Historical passes are not copied to another candidate.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def bounds(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return ({"minimum": min(values), "median": statistics.median(values),
             "maximum": max(values)} if values else None)


def case_records(artifacts, run_id):
    """Join closed follow-ups without overwriting historical ledger verdicts."""
    records = load(artifacts/'continuation-ledger.json', [])
    known = {row['case'] for row in records}
    records = [{**row, 'record_origin': 'main_ledger'} for row in records]
    closed_guards = load(artifacts/'closed-case-guards.json', {}).get('cases', {})
    for folder in sorted((artifacts/'runs'/run_id).glob('*')):
        if folder.name in known:
            continue
        guard = load(folder/'status.json', closed_guards.get(folder.name, {}))
        if 'supervisor_exit_code' not in guard:
            continue  # Never render a live/partial case as a finished result.
        reports = [load(p) for p in sorted(folder.glob('raw-*/report.json'))]
        reports = [r for r in reports if r]
        report = reports[-1] if reports else {}
        diagnostic = any(report.get(key) for key in [
            'diagnostic_hook_injected', 'diagnostic_opencl_intercept_injected',
            'diagnostic_crash_tracer_injected', 'diagnostic_fire_input_capture_injected'])
        state = guard.get('status', 'REQUIRES_REVIEW')
        if diagnostic:
            verdict = 'DIAGNOSTIC_REQUIRES_REVIEW'
        elif state == 'PASS':
            verdict = 'PASS' if report.get('passed') is True else 'FUNCTIONAL_REPORT_REQUIRED'
        else:
            verdict = state
        records.append({**report, 'case': folder.name, 'status': verdict,
                        'guard_status': state, 'probe_passed': report.get('passed'),
                        'record_origin': 'closed_followup'})
    return records


def summarize(root, run_id='remote64-c13'):
    artifacts = root / 'remote-artifacts'
    records = case_records(artifacts, run_id)
    rows = []
    for row in records:
        paths = list((artifacts / 'runs' / run_id / row['case']).glob('raw-*/report.json'))
        reports = [load(p) for p in paths]
        reports = [v for v in reports if v]
        report = reports[-1] if reports else {}
        samples = [v for v in report.get('samples', []) if v.get('sample_phase') == 'steady']
        if not samples:
            samples = report.get('samples', [])
        work = (row.get('steady_completed_work') or report.get('steady_completed_work') or {}).get('per_camera', {})
        health_path = paths[-1].parent/'healthz-final.json' if paths else root/'absent'
        health = load(health_path, {})
        cameras = health.get('telemetry', {}).get('cameras', [])
        temperatures = []
        event_path = artifacts/'runs'/run_id/row['case']/'events.jsonl'
        if event_path.exists():
            with event_path.open() as stream:
                for line in stream:
                    try:
                        temperatures.extend(v['celsius'] for v in json.loads(line).get('temperatures', []))
                    except (ValueError, KeyError):
                        continue
        rows.append({
            'case': row['case'], 'status': row['status'],
            'record_origin': row['record_origin'], 'guard_status': row.get('guard_status'),
            'probe_passed': row.get('probe_passed'), 'capacity_qualified': False,
            'camera_count': report.get('camera_count'),
            'target_fps': report.get('fps_per_camera'),
            'profiles': report.get('camera_workload_profiles'),
            'startup_seconds': report.get('startup_seconds'),
            'steady_seconds': report.get('steady_seconds'),
            'thermal_samples_whole_guard_not_steady_only': bounds(temperatures),
            'completed_fps_by_camera': {k: v['selected_fps'] for k, v in work.items()},
            'minimum_completed_window_ratio': min(
                (v['minimum_window_ratio'] for v in work.values()), default=None),
            'resources': {key: bounds([v.get(key) for v in samples]) for key in [
                'container_memory_mb', 'container_cpu_cores', 'camera_process_pss_mb',
                'gpu_utilization_pct', 'npu_utilization_pct']},
            'stage_timings_by_camera': {v['camera_id']: v.get('use_case_processing')
                                        for v in cameras},
            'source_clock_output_latency_by_camera': {
                v['camera_id']: v.get('outputs', {}).get('production_latency_by_event_type')
                for v in cameras},
            'package_summary': row.get('package_summary', report.get('package_summary')),
            'scene_coverage': row.get('steady_output_coverage', report.get('steady_output_coverage')),
            'fatal_lines': row.get('fatal_lines', report.get('fatal_lines')),
            'local_raw_reports': [str(p) for p in paths],
            'original_remote_report': row.get('report_path'),
        })
    result = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'controller': load(root/'CAMPAIGN_CURRENT.json'),
        'capacity_qualified': False,
        'power_condition': 'CPU turbo disabled, full sixteen logical CPUs eligible',
        'delivery_boundary': 'CV complete-package acceptance at agent API only',
        'resource_notes': [
            'Container CPU is cores, container RAM is cgroup memory; process PSS is separate.',
            'GPU/NPU utilization fields retain their node-wide health metric scope. GPU active residency is not compute-engine utilization.',
            'Do not sum overlapping stage timings or interpret scheduler/model wall time as hardware busy time.',
            'Source-clock latency is not independent source-pixel freshness qualification.',
            'Repeated original footage across unique TCP connections is not a diverse physical-camera dataset.',
        ],
        'frontiers': load(artifacts/'continuation-frontier.json', {}), 'cases': rows,
    }
    (root/'RESULTS_CURRENT.json').write_text(json.dumps(result, indent=2)+'\n')
    lines = ['# Current '+run_id+' workload measurements', '',
             'Conditional CPU turbo-off tests; capacity qualification is unfinished. '
             'S means scene only. SA enables all eleven apps; actual positive work depends on footage.', '',
             'Short-search acceptance requires each camera to average at least95% of target '
             'and every sampled window at least90%, plus independent scene-package coverage checks. '
             'These counts are not exact uninterrupted FPS guarantees.', '']
    if result['frontiers']:
        lines += ['| Profile | FPS target per camera | Last short passing count | First short failing count |',
                  '|---|---:|---:|---:|']
        for key, frontier in result['frontiers'].items():
            profile, fps = key.rsplit('-', 1)
            lines.append(f"| {profile} | {fps} | {frontier['last_short_passing_count']} | "
                         f"{frontier['first_short_failing_count']} |")
        lines.append('')
    lines += [
             '| Case | Verdict | Cameras × target FPS | Completed FPS range per camera | Peak sampled container MiB |',
             '|---|---|---|---|---|']
    for row in rows:
        fps = list(row['completed_fps_by_camera'].values())
        measured = f'{min(fps):.3f}–{max(fps):.3f}' if fps else 'See recovery/functional report'
        memory = row['resources']['container_memory_mb']
        memory = f"{memory['maximum']:.1f}" if memory else '—'
        target = f"{row['camera_count']} × {row['target_fps']}" if row['camera_count'] else 'Functional test'
        lines.append(f"| {row['case']} | {row['status']} | {target} | {measured} | {memory} |")
    lines += ['', 'Failures and blocked sources are retained. A short PASS is not sustained capacity. '
              'Raw reports, per-camera windows, scene image/vector coverage and failure details are linked in RESULTS_CURRENT.json.',
              '', ('The completed one-hour run is listed above. Throughput PASS does not establish flat memory or qualify other workloads.'
                   if any((r['steady_seconds'] or 0) >= 3600 for r in rows) else
                   'The one-hour run still requires its own completed result.'),
              '', 'Closed follow-ups are included separately from the main ledger. Diagnostic runs do not grant capacity; '
              'FUNCTIONAL_REPORT_REQUIRED means inspect the dedicated report, not that a passing launcher proves behavior. '
              'Omitted canonical scenarios remain unqualified.']
    (root/'RESULTS_CURRENT.md').write_text('\n'.join(lines)+'\n')
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--run-id', default='remote64-c13')
    args = parser.parse_args()
    print(json.dumps({'cases_summarized': summarize(args.root, args.run_id)}))
