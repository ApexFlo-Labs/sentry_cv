#!/usr/bin/env python3
"""Guarded native ANPR refusal/retry regression against an explicit image."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--case', default='native-policy-admission-SVX1x5')
    parser.add_argument('--lease-held-by-parent', action='store_true')
    args = parser.parse_args()
    r = args.root.resolve()
    if args.lease_held_by_parent:
        parent = Path('/proc', str(os.getppid()), 'cmdline').read_bytes()
        assert str(r / 'bootstrap_native.py').encode() in parent, 'Only the lease-owning bootstrap may delegate'
    else:
        lease = (r / 'POSTCHECKS.lock').open('a')
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not (r / 'STOP').exists()
    assert Path('/sys/devices/system/cpu/intel_pstate/no_turbo').read_text().strip() == '1'
    for unit in ['k3s.service', 'apexfabric-control.service']:
        assert subprocess.run(['systemctl', 'is-active', unit], capture_output=True).returncode != 0
    directory = r / 'artifacts' / ('launch-' + args.case)
    directory.mkdir(exist_ok=False)
    expected = {'capacity-01': {'events': {'plate_read': 1, 'plate_outcome': 1}}}
    command = ['taskset', '-c', '14,15', 'python3',
        'scripts/isolated_validation/run_guarded_case.py', '--root', str(r / 'artifacts/runs'),
        '--run-id', args.run_id, '--case', args.case, '--engine', 'docker',
        '--min-free-disk-mib', '6144', '--min-available-ram-mib', '4096',
        '--max-attempts', '3', '--max-runtime', '900', '--sample-interval', '.5',
        '--hot-samples', '9', '--max-cooldown', '6000', '--',
        'taskset', '-c', '0-15', 'python3', 'scripts/v8_capacity_probe.py',
        '--engine', 'docker', '--image', args.image, '--cameras', '1', '--fps', '5',
        '--port', '18380', '--startup-timeout', '360', '--warmup-seconds', '20',
        '--steady-seconds', '120', '--sample-seconds', '10', '--activity', 'active',
        '--memory-limit-mib', '24576', '--capture-gallery-limit', '128',
        '--capture-gallery-interval-seconds', '10', '--profile', 'SVX',
        '--recording', str(r / 'fixtures/ANPR.mp4'),
        '--expected-outputs-json', json.dumps(expected), '--diagnostic-hook',
        str(r / 'repo/scripts/isolated_validation/policy_admission_fault_hook.py')]
    (directory / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    (r / 'CAMPAIGN_CURRENT.json').write_text(json.dumps({
        'utc': datetime.now(timezone.utc).isoformat(), 'image': args.image,
        'controller_pid': os.getpid(), 'status': 'RUNNING', 'case': args.case,
        'stage': 'native_policy_admission_fault', 'capacity_qualified': False,
    }) + '\n')
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    env['PATH'] = '/home/admin1/sentry_cv/.venv/bin:' + env['PATH']
    child = None
    bridge = None

    def stop(*_):
        if child is not None and child.poll() is None:
            child.terminate()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, stop)
    with (directory / 'launch.log').open('w') as log:
        try:
            child = subprocess.Popen(command, cwd=r / 'repo', env=env,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            bridge = subprocess.Popen(['taskset', '-c', '14,15', 'python3',
                'scripts/isolated_validation/remote_case_bridge.py', '--root', str(r),
                '--run-id', args.run_id, '--case', args.case, '--guard-pid', str(child.pid),
                '--owner', '1000', '--seconds', '21000'], cwd=r / 'repo', env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            code = child.wait(timeout=21000)
            bridge.wait(timeout=120)
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=120)
            if bridge is not None and bridge.poll() is None:
                bridge.terminate()
                bridge.wait(timeout=120)
    closed = r / 'artifacts/runs' / args.run_id / args.case
    guard = json.loads((closed / 'status.json').read_text())
    assert code == 0 and guard['status'] == 'PASS', 'Native fault probe failed; inspect preserved raw evidence'
    attempt = guard['attempt']
    matching = [v for v in guard['attempts'] if v['attempt'] == attempt]
    assert len(matching) == 1 and matching[0]['cleanup_ok'] is True
    raw = None
    for line in (closed / f'attempt-{attempt}.stdout.log').read_text().splitlines():
        try:
            path = json.loads(line).get('artifact_directory')
        except (ValueError, AttributeError):
            continue
        if path and Path(path).parent == Path('/tmp'):
            raw = closed / ('raw-' + Path(path).name)
    assert raw is not None and (raw / 'report.json').is_file()
    result = r / 'artifacts/native-policy-admission-result.json'
    analysis = ['taskset', '-c', '14,15', 'python3',
        'scripts/isolated_validation/analyze_policy_admission.py', str(raw),
        '--image', args.image, '--output', str(result)]
    (directory / 'analysis-command.json').write_text(json.dumps(analysis, indent=2) + '\n')
    with (directory / 'analysis.log').open('w') as log:
        subprocess.run(analysis, cwd=r / 'repo', env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    proof = {'utc': datetime.now(timezone.utc).isoformat(), 'image': args.image,
             'case': args.case, 'result': str(result), 'passed': True,
             'scope': 'Native one-shot local admission refusal recovery; not capacity or OCR accuracy'}
    (r / 'NATIVE_POLICY_ADMISSION_PASS.json').write_text(json.dumps(proof, indent=2) + '\n')
    for path in [result, r / 'NATIVE_POLICY_ADMISSION_PASS.json']:
        os.chown(path, 1000, 1000)


if __name__ == '__main__':
    main()
