#!/usr/bin/env python3
"""Bounded Linux perf sampling of one explicitly owned native camera worker.

External diagnostic instrumentation; never a clean capacity qualification.
Does not change gates, model settings, task placement or host power settings.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--guard-status', type=Path, required=True)
    p.add_argument('--phase-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18380)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic()+500
    warm_since = None
    while time.monotonic() < deadline:
        status = json.loads(args.guard_status.read_text()) if args.guard_status.exists() else {}
        if status.get('status') in {'PASS', 'FAIL', 'BLOCKED_THERMAL', 'BLOCKED_RESOURCE', 'BLOCKED_TIMEOUT'}:
            (args.output/'result.json').write_text(json.dumps({'captured': False, 'guard_status': status['status']}))
            return 2
        phase = json.loads(args.phase_file.read_text()) if args.phase_file.exists() else {}
        if phase.get('phase') not in ('warmup', 'steady'):
            time.sleep(.5)
            continue
        warm_since = warm_since or time.monotonic()
        if time.monotonic()-warm_since < 8:
            time.sleep(.5)
            continue
        with urlopen(f'http://127.0.0.1:{args.port}/healthz', timeout=2) as response:
            health = json.load(response)
        cameras = health.get('telemetry', {}).get('cameras', [])
        if len(cameras) != 1:
            raise RuntimeError('single-camera diagnostic expected')
        namespace_pid = int(cameras[0]['pid'])
        ids = subprocess.check_output(['podman', 'ps', '-q', '--filter', 'label=sentinel.validation_run='+args.run_id,
                                      '--filter', 'label=sentinel.validation_case='+args.case], text=True).split()
        if len(ids) != 1:
            raise RuntimeError('exact owned running container required')
        top = subprocess.check_output(['podman', 'top', ids[0], 'hpid', 'pid', 'comm'], text=True)
        matches = [line.split() for line in top.splitlines()[1:]
                   if len(line.split()) >= 3 and line.split()[1] == str(namespace_pid)]
        if len(matches) != 1:
            raise RuntimeError('exact host/namespace worker mapping required')
        host_pid = int(matches[0][0])
        root = Path(f'/proc/{host_pid}/root')
        (args.output/'worker-mappings.txt').write_text(Path(f'/proc/{host_pid}/maps').read_text())
        (args.output/'health-before.json').write_text(json.dumps(health, indent=2)+'\n')
        command = ['sudo', '-n', 'timeout', '--signal=INT', '35', 'perf', 'record',
                   '-N', '-e', 'cpu-clock:u', '-F', '49', '-g', '--call-graph', 'dwarf,8192',
                   '-p', str(host_pid), '-o', str(args.output/'perf.data')]
        record = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic': time.monotonic(),
                  'container_id': ids[0], 'namespace_pid': namespace_pid,
                  'host_pid': host_pid, 'command': command, 'scope': __doc__}
        (args.output/'launch.json').write_text(json.dumps(record, indent=2)+'\n')
        with (args.output/'perf-record.log').open('wb') as out:
            sampled = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, timeout=50)
        report_command = ['sudo', '-n', 'perf', 'report', '--stdio', '--stdio-color', 'never',
                          '--no-children', '--percent-limit', '.25', '--sort', 'dso,symbol',
                          '-i', str(args.output/'perf.data')]
        if root.exists():
            report_command += ['--symfs', str(root)]
        with (args.output/'perf-report.txt').open('wb') as out:
            report = subprocess.run(report_command, stdout=out, stderr=subprocess.STDOUT, timeout=30)
        record.update(record_exit_code=sampled.returncode, report_exit_code=report.returncode,
                      report_command=report_command, ended_utc=datetime.now(timezone.utc).isoformat(),
                      worker_live_at_report=root.exists(), captured=(args.output/'perf.data').is_file())
        (args.output/'result.json').write_text(json.dumps(record, indent=2)+'\n')
        return 0 if record['captured'] and report.returncode == 0 else 2
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
