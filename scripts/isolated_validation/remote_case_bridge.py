#!/usr/bin/env python3
"""Checkpoint one owned remote guard, including user STOP and raw evidence.

Does not launch workloads or attach debuggers. Run as the same user as the guard.
An optional evidence owner permits an unprivileged SSH account to mirror files.
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
    temp = path.with_suffix('.bridge.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temp, path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--guard-pid', type=int, required=True)
    p.add_argument('--owner', type=int)
    p.add_argument('--seconds', type=int, default=7200)
    a = p.parse_args()
    guard = a.root/'artifacts/runs'/a.run_id/a.case
    command = Path(f'/proc/{a.guard_pid}/cmdline').read_bytes().split(b'\0')
    tokens = [v.decode() for v in command if v]
    assert any(v.endswith('/run_guarded_case.py') for v in tokens)
    assert tokens[tokens.index('--case')+1] == a.case
    assert tokens[tokens.index('--run-id')+1] == a.run_id
    start = Path(f'/proc/{a.guard_pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    deadline = time.monotonic()+a.seconds
    memory_started, raws = set(), set()
    stopped = False
    while time.monotonic() < deadline:
        try:
            d = json.loads((guard/'status.json').read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(1)
            continue
        if a.owner is not None:
            # The root guard creates this run directory as 0700. Owning the
            # child case alone does not let the SSH evidence mirror traverse it.
            os.chown(guard.parent, a.owner, a.owner)
        if (a.root/'STOP').exists() and not stopped:
            proc = Path(f'/proc/{a.guard_pid}')
            if proc.exists() and proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19] == start:
                os.kill(a.guard_pid, signal.SIGTERM)
            stopped = True
        for stdout in guard.glob('attempt-*.stdout.log'):
            for row in stdout.read_text().splitlines():
                try:
                    raw = json.loads(row).get('artifact_directory')
                except (ValueError, AttributeError):
                    continue
                if not raw or not re.fullmatch(r'/tmp/sentry-v8-capacity-\d+\.[\w-]+', raw):
                    continue
                raws.add(raw)
                cidfile = Path(raw)/'container-id.txt'
                if raw not in memory_started and cidfile.is_file():
                    target = guard/(stdout.stem+'.cgroup.jsonl.gz')
                    with (guard/(stdout.stem+'.cgroup.log')).open('w') as log:
                        subprocess.Popen(['python3', 'scripts/isolated_validation/cgroup_memory_observer.py',
                            '--engine', 'docker', '--run-id', a.run_id, '--case', a.case,
                            '--container-id-file', str(cidfile), '--output', str(target),
                            '--seconds', str(min(a.seconds, 10800))], stdout=log, stderr=subprocess.STDOUT)
                    memory_started.add(raw)
        terminal = 'supervisor_exit_code' in d
        summary = {'utc': datetime.now(timezone.utc).isoformat(), 'case': a.case,
                   'phase': d.get('event'), 'guard_status': d.get('status'),
                   'guard_pid': a.guard_pid, 'raw_directories': sorted(raws),
                   'capacity_qualified': False, 'terminal': terminal,
                   'scope': 'Controller status; capacity requires report and all declared assertions'}
        image_args = d.get('command', [])
        if '--image' in image_args:
            summary['image_config_id'] = image_args[image_args.index('--image')+1]
        if terminal:
            for raw in sorted(raws):
                destination = guard/('raw-'+Path(raw).name)
                if not destination.exists():
                    cmd = ['python3', 'scripts/isolated_validation/archive_case.py',
                           '--source', raw, '--destination', str(destination)]
                    if not (Path(raw)/'report.json').exists():
                        cmd += ['--partial-reason', 'Guard ended: '+str(d.get('status'))]
                    subprocess.run(cmd, check=True)
                log = Path(raw)/'container.log'
                if log.is_file():
                    matches = [line for line in log.read_text(errors='replace').splitlines()
                               if re.search(r'CPU affinity check failed|No CPU is eligible|'
                                            r'CL_OUT_OF_RESOURCES|camera_thread_unhandled_exception|'
                                            r'native_graph_watchdog_fenced|secondary inference failed', line)]
                    clean = [re.sub(r'\b(?:rtsps?|https?|tcp)://[^\s\"\'<>]+',
                                    '<redacted-url>', line, flags=re.I) for line in matches]
                    atomic(guard/('diagnostic-'+Path(raw).name+'.json'), {
                        'guard_status': d.get('status'), 'runtime_error_lines': clean,
                        'scope': 'Independent runtime findings; thermal termination does not erase earlier errors'})
            summary['archived'] = True
        for name in ['CURRENT.json', 'STATUS.json']:
            atomic(a.root/name, summary)
        if a.owner is not None:
            for parent in [guard, *guard.rglob('*')]:
                if parent.is_symlink():
                    continue
                try:
                    os.chown(parent, a.owner, a.owner)
                except FileNotFoundError:
                    pass
        if terminal:
            return 0
        time.sleep(2)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
