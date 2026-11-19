#!/usr/bin/env python3
"""Read-only host/health observer; does not start/stop workloads or change power."""
import argparse
from datetime import datetime, timezone
import gzip
import importlib.util
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.request import urlopen

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--seconds', type=float, default=7200)
    parser.add_argument('--current-file', type=Path,
                        help='Campaign CURRENT file when saving into a fresh observer segment')
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    destination = args.root / 'host-observations.jsonl.gz'
    if destination.exists():
        parser.error('use a fresh evidence directory; existing samples are immutable')
    source = Path(__file__).resolve().parents[2] / 'edge_runtime/solution_packs/sporada_secure/runtime_v8/services/worker/pipeline/resource_metrics.py'
    spec = importlib.util.spec_from_file_location('observer_resource_metrics', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    accelerator = module.AcceleratorSampler()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    (args.root / 'observer-pid.txt').write_text(str(os.getpid()) + '\n')
    psutil.cpu_percent(None)
    started = time.monotonic()
    with gzip.open(destination, 'xt', encoding='utf-8') as log:
        while not stop.is_set() and not (args.root / 'STOP_OBSERVER').exists() and time.monotonic() - started < args.seconds:
            tick = time.monotonic()
            record = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic': tick,
                      'hostname': os.uname().nodename,
                      'scope': 'Nonisolated host; GPU/NPU counters are node-wide, not CV-only',
                      'host_cpu_percent': psutil.cpu_percent(None),
                      'host_memory': psutil.virtual_memory()._asdict(),
                      'swap': psutil.swap_memory()._asdict(),
                      'temperatures': {k: [x._asdict() for x in v] for k, v in psutil.sensors_temperatures().items()},
                      'accelerators': accelerator.sample(),
                      'observer_rss_bytes': psutil.Process().memory_info().rss,
                      'throttling': {str(p): p.read_text().strip() for p in Path('/sys/devices/system/cpu').glob('cpu*/thermal_throttle/*_throttle_count') if os.access(p, os.R_OK)}}
            try:
                record['case'] = json.loads((args.current_file or args.root / 'CURRENT.json').read_text())
            except (OSError, ValueError):
                record['case'] = None
            try:
                with urlopen(f'http://127.0.0.1:{args.port}/healthz', timeout=.7) as response:
                    record['health'] = json.load(response)
            except Exception as exc:
                record['health_error'] = type(exc).__name__
            log.write(json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n')
            log.flush()
            stop.wait(max(.05, 1 - (time.monotonic() - tick)))
    (args.root / 'observer-finished.json').write_text(json.dumps({
        'utc': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - started,
        'scope': 'Measurement capture completed; does not confer a test PASS.'}, indent=2) + '\n')


if __name__ == '__main__':
    main()
