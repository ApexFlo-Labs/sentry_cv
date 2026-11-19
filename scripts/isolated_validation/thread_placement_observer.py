#!/usr/bin/env python3
"""Read-only per-thread CPU/affinity evidence for one owned CV container.

No task affinity is modified. A broad mask after the release proof invalidates
an efficiency-core-only claim even if a command requested that placement.
"""
import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container-id-file', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--expected-cpus', required=True)
    p.add_argument('--release-proof', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=1000)
    args = p.parse_args()
    if not 1 <= args.seconds <= 7200:
        p.error('bounded duration required')
    expected = set(map(int, args.expected_cpus.split(',')))
    data = json.loads(subprocess.check_output(
        ['podman', 'inspect', args.container_id_file.read_text().strip()], text=True))[0]
    labels = data['Config']['Labels']
    assert labels['sentinel.validation_run'] == args.run_id
    assert labels['sentinel.validation_case'] == args.case
    assert data['State']['Running']
    init = int(data['State']['Pid'])
    relative = next(s.split('::', 1)[1] for s in Path(f'/proc/{init}/cgroup').read_text().splitlines() if s.startswith('0::'))
    assert relative != '/'
    cgroup = Path('/sys/fs/cgroup')/relative.lstrip('/')
    ticks = os.sysconf('SC_CLK_TCK')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic()+args.seconds
    with gzip.open(args.output, 'xt') as out:
        while time.monotonic() < deadline and Path(f'/proc/{init}').exists():
            now = time.monotonic()
            tids, records = set(), []
            try:
                pids = {int(value) for source in cgroup.rglob('cgroup.procs')
                        for value in source.read_text().split()}
            except OSError:
                break
            for pid in pids:
                try:
                    tasks = list(Path(f'/proc/{pid}/task').iterdir())
                except OSError:
                    continue
                for task in tasks:
                    try:
                        tid = int(task.name)
                        if tid in tids:
                            continue
                        tids.add(tid)
                        stat = (task/'stat').read_text().rsplit(')', 1)[1].split()
                        allowed = sorted(os.sched_getaffinity(tid))
                        records.append({'pid': pid, 'tid': tid, 'name': (task/'comm').read_text().strip(),
                                        'cpu_seconds': (int(stat[11])+int(stat[12]))/ticks,
                                        'last_processor': int(stat[36]), 'allowed_cpus': allowed,
                                        'allowed_within_expected': set(allowed) <= expected})
                    except (OSError, ProcessLookupError):
                        continue
            released = args.release_proof.is_file()
            row = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic': now,
                   'container_id': data['Id'], 'scope': __doc__,
                   'after_release_proof': released, 'expected_cpus': sorted(expected),
                   'broader_after_release': [r['tid'] for r in records
                                             if released and not r['allowed_within_expected']],
                   'threads': records}
            out.write(json.dumps(row, separators=(',', ':'))+'\n')
            out.flush()
            time.sleep(max(.05, 1-(time.monotonic()-now)))


if __name__ == '__main__':
    main()
