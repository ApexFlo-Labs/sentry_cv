#!/usr/bin/env python3
"""Read-only process CPU attribution, frequencies and available power counters.

Names/PIDs only: no command lines, URLs, environment or credentials. Values
cover this shared host, not exclusively CV. Does not change power settings.
"""
import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import signal
import threading
import time

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=10800)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 10800:
        parser.error('bounded duration required')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    previous, last = {}, time.monotonic()
    with gzip.open(args.output, 'xt') as out:
        deadline = last+args.seconds
        while not stopped.is_set() and time.monotonic() < deadline:
            now = time.monotonic()
            elapsed = max(.001, now-last)
            rows, current = [], {}
            for process in psutil.process_iter(
                    ['pid', 'name', 'create_time', 'cpu_times', 'num_threads', 'memory_info']):
                try:
                    v = process.info
                    key = (v['pid'], v['create_time'])
                    cpu = v['cpu_times'].user+v['cpu_times'].system
                    current[key] = cpu
                    rows.append({'pid': v['pid'], 'name': v['name'],
                                 'created_at_epoch': v['create_time'],
                                 'cpu_seconds': cpu, 'threads': v['num_threads'],
                                 'rss_bytes': v['memory_info'].rss,
                                 'cpu_cores': max(0, cpu-previous[key])/elapsed if key in previous else None})
                except (psutil.Error, AttributeError, TypeError):
                    continue
            def readable(paths):
                result = {}
                for path in paths:
                    try:
                        result[str(path)] = path.read_text().strip()
                    except (OSError, PermissionError):
                        pass
                return result
            record = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic': now,
                      'interval_seconds': elapsed, 'scope': __doc__,
                      'observer_pid': os.getpid(), 'processes': rows,
                      'cpu_frequency_khz': readable(Path('/sys/devices/system/cpu').glob(
                          'cpu*/cpufreq/scaling_cur_freq')),
                      'power_energy_uj': readable(Path('/sys/class/powercap').glob(
                          'intel-rapl*/energy_uj'))}
            out.write(json.dumps(record, separators=(',', ':'))+'\n')
            out.flush()
            previous, last = current, now
            stopped.wait(max(.05, 1-(time.monotonic()-now)))


if __name__ == '__main__':
    main()
