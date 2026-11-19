#!/usr/bin/env python3
"""Read-only memory breakdown for one explicitly owned live CV test container."""
import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import subprocess
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container-id-file', type=Path, required=True)
    p.add_argument('--engine', choices=['podman', 'docker'], default='podman')
    p.add_argument('--run-id', required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=float, default=4200)
    a = p.parse_args()
    cid = a.container_id_file.read_text().strip()
    if not re.fullmatch('[0-9a-f]{12,64}', cid) or not 0 < a.seconds <= 10800:
        raise ValueError('exact saved container ID and bounded duration required')
    data = json.loads(subprocess.check_output([a.engine, 'inspect', cid], text=True))[0]
    labels = data['Config']['Labels']
    assert labels.get('sentinel.validation_run') == a.run_id
    assert labels.get('sentinel.validation_case') == a.case
    assert data['State']['Running'] or data['State'].get('Paused'), 'owned container must be alive'
    pid = int(data['State']['Pid'])
    proc = Path('/proc')/str(pid)
    start_ticks = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]
    path = next(l.split('::', 1)[1] for l in proc.joinpath('cgroup').read_text().splitlines() if l.startswith('0::'))
    assert path != '/'
    cgroup = Path('/sys/fs/cgroup')/path.lstrip('/')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    names = ['memory.current', 'memory.peak', 'memory.max', 'memory.swap.current',
             'memory.stat', 'memory.events', 'memory.pressure', 'cpu.stat', 'cpu.max', 'io.stat', 'cgroup.procs']
    deadline = time.monotonic()+a.seconds
    with gzip.open(a.output, 'xt') as out:
        while time.monotonic() < deadline:
            tick = time.monotonic()
            try:
                if proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19] != start_ticks:
                    break
                raw = {name: (cgroup/name).read_text() if (cgroup/name).is_file() else None for name in names}
            except FileNotFoundError:
                break
            record = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic': tick,
                      'scope': 'Exact owned container cgroup; RAM cache/anon/kernel breakdown, not summed per-camera RSS',
                      'container_id': data['Id'], 'host_init_pid': pid, 'cgroup': path,
                      'run_id': a.run_id, 'case': a.case, 'raw': raw}
            out.write(json.dumps(record, separators=(',', ':'))+'\n')
            out.flush()
            time.sleep(max(.01, 1-(time.monotonic()-tick)))


if __name__ == '__main__':
    main()
