#!/usr/bin/env python3
"""Read-only physical-link/counter samples on the test host.

Counters are host interface totals, not CV-exclusive. Their deltas must be
aligned to the same-host monotonic test windows. No traffic capture, URLs,
network configuration changes or inference work is performed.
"""
import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import signal
import threading
import time


def snapshot():
    interfaces = []
    for path in sorted(Path('/sys/class/net').iterdir()):
        if not (path/'device').exists():
            continue
        row = {'interface': path.name}
        for name in ['speed', 'duplex', 'carrier', 'operstate',
                     'statistics/rx_bytes', 'statistics/tx_bytes',
                     'statistics/rx_errors', 'statistics/rx_dropped',
                     'statistics/tx_errors', 'statistics/tx_dropped']:
            try:
                raw = (path/name).read_text().strip()
                row[name] = int(raw) if raw.lstrip('-').isdigit() else raw
            except OSError:
                row[name] = None
        interfaces.append(row)
    return {'utc': datetime.now(timezone.utc).isoformat(),
            'monotonic': time.monotonic(), 'interfaces': interfaces}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=86400)
    parser.add_argument('--interval', type=float, default=1)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400 or not .5 <= args.interval <= 60:
        parser.error('bounded duration and interval required')
    args.output.mkdir(exist_ok=False)
    stop = threading.Event()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: stop.set())
    (args.output/'scope.json').write_text(json.dumps({'scope': __doc__}, indent=2)+'\n')
    deadline = time.monotonic()+args.seconds
    with gzip.open(args.output/'samples.jsonl.gz', 'wt') as stream:
        while not stop.is_set() and time.monotonic() < deadline:
            record = snapshot()
            stream.write(json.dumps(record)+'\n')
            stream.flush()
            stop.wait(args.interval)


if __name__ == '__main__':
    main()
