#!/usr/bin/env python3
"""Continue guarded frozen-image checks on the isolated host, with power scope.

This controller never changes the image, policy, device configuration or power.
It requires the previously recorded turbo-off diagnostic condition. A probe PASS
is bounded workload evidence, never automatic full production qualification.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--sources-json', type=Path, required=True)
    p.add_argument('--run-id', default='remote64-c12')
    p.add_argument('--baseline-case', default='all-usecases-local-people1x5-turbo-off')
    p.add_argument('--recovery-upstream')
    p.add_argument('--recovery-ca', type=Path)
    p.add_argument('--recovery-profile', choices=['S','SVX','SA'], default='S')
    p.add_argument('--recovery-fps', type=int, default=4)
    p.add_argument('--resume', action='store_true', help='Preserve terminal ledger rows only when their complete commands match')
    p.add_argument('--retain-known-native-failure', action='append', default=[],
                   help='Exact previously failed case to retain while measuring independent points; never changes its FAIL or qualifies release')
    p.add_argument('--count-plan-json', type=Path,
                   help='Explicit per-profile/FPS count lists for independently repeating prior boundaries; results are never inherited')
    p.add_argument('--prefetch-fault-regression', action='store_true',
                   help='Also inject missing PTS at actual retained-surface consumption before capacity/soak')
    p.add_argument('--isolation-case-name',default='outage-all2x5-availability-oracle',
                   help='Fresh case name for an explicitly corrected/repeated isolation test; old results remain immutable')
    p.add_argument('--continue-independent-on-failure',action='store_true',
                   help='Retain nonfatal functional FAILs and continue independent checks; never grants qualification')
    a = p.parse_args()
    r = a.root.resolve()
    repo = r/'repo'
    source_urls = json.loads(a.sources_json.read_text())
    assert len(source_urls) == 50
    assert re.fullmatch(r'outage-[a-z0-9-]{1,100}',a.isolation_case_name)
    count_plan = json.loads(a.count_plan_json.read_text()) if a.count_plan_json else {}
    for key, counts in count_plan.items():
        assert key in {f'{profile}-{fps}' for profile in ['S','SA'] for fps in [5,8,10]}
        assert counts and counts[0] == 1 and counts == sorted(set(counts))
        assert all(isinstance(n,int) and not isinstance(n,bool) and 1 <= n <= 50 for n in counts)
    ledger = r/'artifacts/continuation-ledger.json'
    current = r/'CAMPAIGN_CURRENT.json'
    assert a.resume or not ledger.exists(), 'New campaign controller requires a fresh ledger'
    records = json.loads(ledger.read_text()) if a.resume and ledger.exists() else []
    cancelled = [False]
    active = [None]

    def stop(*_):
        cancelled[0] = True
        if active[0] is not None and active[0].poll() is None:
            active[0].send_signal(signal.SIGTERM)
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, stop)

    def state(**fields):
        atomic(current, {'utc': datetime.now(timezone.utc).isoformat(),
                         'controller_pid': os.getpid(), 'power_scope': 'CPU turbo disabled',
                         'image': a.image, 'capacity_qualified': False, **fields})

    def execute(name, extra=None, *, native_mode=None):
        if cancelled[0] or (r/'STOP').exists():
            raise InterruptedError('User stop')
        assert Path('/sys/devices/system/cpu/intel_pstate/no_turbo').read_text().strip() == '1'
        for unit in ['k3s.service', 'apexfabric-control.service']:
            assert subprocess.run(['systemctl', 'is-active', unit], capture_output=True).returncode != 0
        case = 'turbo-off-next-'+name
        directory = r/'artifacts/runs'/a.run_id/case
        previous = next((x for x in records if x['case'] == case), None)
        child = ['taskset', '-c', '0-15', 'python3']
        if native_mode:
            output = r/'artifacts'/('recovery-'+name)
            child += ['scripts/isolated_validation/v8_recovery_image_validation.py',
                      '--engine', 'docker', '--image', a.image, '--output', str(output),
                      '--mode', native_mode, '--profile', a.recovery_profile, '--fps', str(a.recovery_fps),
                      '--port', '18381']
            if a.recovery_upstream:
                child += ['--upstream-url',a.recovery_upstream]
            if a.recovery_ca:
                child += ['--upstream-ca-file',str(a.recovery_ca)]
        else:
            child += ['scripts/v8_capacity_probe.py', '--engine', 'docker', '--image', a.image,
                      '--port', '18380', '--startup-timeout', '360', '--warmup-seconds', '20',
                      '--steady-seconds', '120', '--sample-seconds', '10', '--activity', 'active',
                      '--memory-limit-mib', '24576', '--capture-gallery-limit', '128',
                      '--capture-gallery-interval-seconds', '10', *(extra or [])]
        cmd = ['taskset', '-c', '14,15', 'python3',
               'scripts/isolated_validation/run_guarded_case.py', '--root', str(r/'artifacts/runs'),
               '--run-id', a.run_id, '--case', case, '--engine', 'docker',
               '--min-free-disk-mib', '6144', '--min-available-ram-mib', '4096',
               '--max-attempts', '3', '--max-runtime', '5400', '--sample-interval', '.5',
               '--hot-samples', '9', '--max-cooldown', '86400', '--', *child]
        if previous is not None:
            assert a.resume, 'Reusing a row requires explicit resume'
            saved = json.loads((r/'artifacts'/(case+'-command.json')).read_text())['command']
            assert saved == cmd, 'Previous command/image differs; never reuse a stale result'
            verdict = json.loads((directory/'status.json').read_text())
            assert 'supervisor_exit_code' in verdict and verdict['status'] == previous['status']
            assert previous['status'] in ['PASS','FAIL'], 'Excluded/partial attempts require review'
            if previous.get('fatal_lines'):
                assert case in a.retain_known_native_failure, 'Unreviewed prior native failure'
                assert previous['status'] == 'FAIL' and not previous['probe_passed']
                previous['known_native_failure_retained'] = True
                previous['failure_scope'] = 'Unresolved native crash, not a hardware capacity limit; independent measurements continue'
                atomic(ledger, records)
            return previous
        assert not directory.exists(), 'Never overwrite prior attempts'
        atomic(r/'artifacts'/(case+'-command.json'), {'command': cmd,
            'scope': 'Conditional turbo-off workload, not stock-power maximum or recognition accuracy'})
        state(status='RUNNING', case=case, completed=len(records))
        env = dict(os.environ, PYTHONUNBUFFERED='1')
        env['PATH'] = '/home/admin1/sentry_cv/.venv/bin:'+env['PATH']
        with (r/'artifacts'/(case+'-controller.log')).open('w') as log:
            guard = subprocess.Popen(cmd, cwd=repo, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            active[0] = guard
            bridge = subprocess.Popen(['taskset', '-c', '14,15', 'python3',
                'scripts/isolated_validation/remote_case_bridge.py', '--root', str(r),
                '--run-id', a.run_id, '--case', case, '--guard-pid', str(guard.pid),
                '--owner', '1000', '--seconds', '172800'], cwd=repo, env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while guard.poll() is None:
                if cancelled[0] or (r/'STOP').exists():
                    guard.send_signal(signal.SIGTERM)
                time.sleep(1)
            try:
                bridge.wait(timeout=120)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Archive bridge did not finish; inspect before further load')
        active[0] = None
        result = json.loads((directory/'status.json').read_text())
        raw_report = None
        for f in sorted(directory.glob('attempt-*.stdout.log'), key=lambda p: p.stat().st_mtime):
            for line in f.read_text().splitlines():
                try:
                    raw = json.loads(line).get('artifact_directory')
                except (ValueError, AttributeError):
                    continue
                if raw and re.fullmatch(r'/tmp/sentry-v8-capacity-\d+\.[\w-]+', raw):
                    candidate = Path(raw)/'report.json'
                    if candidate.exists():
                        raw_report = json.loads(candidate.read_text())
        if native_mode:
            candidate = r/'artifacts'/('recovery-'+name)/'report.json'
            if candidate.exists():
                raw_report = json.loads(candidate.read_text())
        row = {'case': case, 'status': result['status'], 'guard_exit_code': guard.returncode,
               'scope': 'turbo-off conditional; replay location is recorded by source_mode and command',
               'source_mode': (raw_report or {}).get('source_mode'),
               'report_path': str(candidate) if raw_report else None,
               'probe_passed': bool(raw_report and raw_report.get('passed')),
               'capacity_qualified': False,
               'package_summary': (raw_report or {}).get('package_summary'),
               'steady_throughput': (raw_report or {}).get('steady_throughput'),
               'steady_completed_work': (raw_report or {}).get('steady_completed_work'),
               'steady_output_coverage': (raw_report or {}).get('steady_output_coverage'),
               'source_utc_sanity': (raw_report or {}).get('source_utc_sanity'),
               'fatal_lines': (raw_report or {}).get('fatal_lines')}
        records.append(row)
        atomic(ledger, records)
        if cancelled[0] or (r/'STOP').exists():
            raise InterruptedError('User stop')
        if result['status'].startswith('BLOCKED'):
            raise RuntimeError('Resource/sensor/thermal/cleanup block requires diagnosis: '+result['status'])
        return row

    try:
        baseline = r/'artifacts/runs'/a.run_id/a.baseline_case/'status.json'
        while True:
            if cancelled[0] or (r/'STOP').exists():
                raise InterruptedError('User stop')
            d = json.loads(baseline.read_text())
            if 'supervisor_exit_code' in d:
                assert d['status'] == 'PASS', 'Native foundation must pass first'
                break
            state(status='WAITING_BASELINE')
            time.sleep(2)
        fixed = [
            ('anpr5', ['--cameras','1','--fps','5','--profile','SVX','--recording',str(r/'fixtures/ANPR.mp4'),
                       '--expected-outputs-json',json.dumps({'capacity-01':{'events':{'plate_read':1}}})]),
            ('fire-clear5-mixed-repeat', ['--cameras','1','--fps','5','--profile','SH','--recording',str(r/'fixtures/fire20-black30-720p10.mp4'),
                            '--activity','mixed',
                            '--expected-outputs-json',json.dumps({'capacity-01':{'events':{'fire_smoke_suspected':1,'fire_smoke_cleared':1}}})]),
            ('all-people8', ['--cameras','1','--fps','8','--profile','SA','--recording',str(r/'fixtures/people1080p30.mp4')]),
            ('all-people10', ['--cameras','1','--fps','10','--profile','SA','--recording',str(r/'fixtures/people1080p30.mp4')]),
            (a.isolation_case_name, ['--cameras','2','--fps','5','--profile','SA','--recording',str(r/'fixtures/people1080p30.mp4'),
                              '--outage-camera','1','--outage-after-seconds','25','--outage-seconds','18']),
        ]
        for name, args in fixed:
            row = execute(name, args)
            if not row['probe_passed']:
                if (name in {'all-people8','all-people10'} and not row['fatal_lines']
                        and (row['steady_throughput'] or {}).get('passed')
                        and (row['steady_output_coverage'] or {}).get('passed')
                        and (row['source_utc_sanity'] or {}).get('passed')):
                    row['completion_capacity_shortfall'] = True
                    atomic(ledger, records)
                    # Retain the failed requested-rate point. Independent
                    # profiles/rates still require measurement; no PASS granted.
                    continue
                if a.continue_independent_on_failure and not row['fatal_lines'] and row['report_path']:
                    row['unresolved_functional_failure']=True
                    atomic(ledger,records)
                    continue
                raise RuntimeError('Functional failure requires review: '+row['case'])
        recovery_modes = ['reconnect','native-hang','bad-first-source']
        if a.prefetch_fault_regression:
            assert a.recovery_profile == 'SA', 'Prefetch regression requires subject analytics'
            recovery_modes.append('prefetch-missing-pts')
        for mode in recovery_modes:
            suffix = ('-controlled-'+a.recovery_profile+'-'+str(a.recovery_fps)
                      if a.recovery_upstream else '')
            row = execute(mode+suffix, native_mode=mode)
            if not row['probe_passed']:
                if a.continue_independent_on_failure and not row['fatal_lines'] and row['report_path']:
                    row['unresolved_recovery_failure']=True
                    atomic(ledger,records)
                    continue
                raise RuntimeError('Recovery failure requires review: '+row['case'])
        frontier = {}
        for profile in ['S','SA']:
            for fps in [5,8,10]:
                last = 0
                failed = None
                standard_counts = [1,2,4,8,12,16,20,24,32,40,50]
                counts = list(count_plan.get(f'{profile}-{fps}', standard_counts))
                # A prior boundary guides the search; it must not cap it when
                # this candidate passes every requested point.
                counts += [n for n in standard_counts if n > counts[-1]]
                for n in counts:
                    row = execute(f'{profile}-{n}x{fps}', [
                        '--cameras',str(n),'--fps',str(fps),'--profile',profile,
                        '--source-urls-json',json.dumps(source_urls[:n])])
                    if not row['probe_passed']:
                        if row['fatal_lines'] and not row.get('known_native_failure_retained'):
                            raise RuntimeError('Native failure requires investigation: '+row['case'])
                        if not last:
                            supported = (profile == 'SA' and any(
                                x.get('completion_capacity_shortfall')
                                and x['case'] == f'turbo-off-next-all-people{fps}'
                                for x in records)
                                and (row['steady_throughput'] or {}).get('passed')
                                and (row['steady_output_coverage'] or {}).get('passed'))
                            if not supported:
                                raise RuntimeError('First external source point failed; diagnose source/runtime before inferring capacity')
                        failed = n
                        break
                    last = n
                if failed is not None:
                    while failed-last > 1:
                        n = (failed+last)//2
                        row = execute(f'{profile}-{n}x{fps}', [
                            '--cameras',str(n),'--fps',str(fps),'--profile',profile,
                            '--source-urls-json',json.dumps(source_urls[:n])])
                        if row['fatal_lines'] and not row.get('known_native_failure_retained'):
                            raise RuntimeError('Native failure requires investigation: '+row['case'])
                        if row['probe_passed']:
                            last = n
                        else:
                            failed = n
                frontier[f'{profile}-{fps}'] = {'last_short_passing_count': last,
                    'first_short_failing_count': failed,
                    'unresolved_native_failure_history': [x['case'] for x in records
                        if x.get('known_native_failure_retained')],
                    'scope': 'dense-people1080p30 TCP, turbo-off; not full qualification'}
                atomic(r/'artifacts/continuation-frontier.json', frontier)
        n = frontier.get('SA-8', {}).get('last_short_passing_count', 0)
        if n:
            execute(f'all-{n}x8-one-hour', ['--cameras',str(n),'--fps','8','--profile','SA',
                    '--source-urls-json',json.dumps(source_urls[:n]),'--steady-seconds','3600'])
        state(status='PLANNED_NATIVE_BATCH_FINISHED_REQUIRES_EVIDENCE_REVIEW', completed=len(records),
              note='This batch is not all282 scenarios. Labelled accuracy, independent latency and omitted cases remain explicit ledger work.')
    except Exception as e:
        state(status='NEEDS_REVIEW' if not cancelled[0] else 'STOPPED',
              reason=type(e).__name__+': '+str(e), completed=len(records))
        raise


if __name__ == '__main__':
    main()
