#!/usr/bin/env python3
"""Timestamp received intel_gpu_top samples; read-only, node-scoped counters."""
import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import signal
import subprocess
import time


class Objects:
    """Accept object streams and JSON arrays emitted by different tool versions."""
    def __init__(self):
        self.pending = ''

    def feed(self, text):
        self.pending += text
        if len(self.pending) > 16*1024*1024:
            raise ValueError('GPU observer JSON exceeded bounded buffer')
        result = []
        while True:
            self.pending = self.pending.lstrip(' \t\r\n[],')
            if not self.pending:
                return result
            try:
                value, end = json.JSONDecoder().raw_decode(self.pending)
            except json.JSONDecodeError:
                return result
            if not isinstance(value, dict):
                raise ValueError('GPU observer expected object samples')
            result.append(value)
            self.pending = self.pending[end:]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True, help='Fresh directory')
    p.add_argument('--seconds', type=int, default=10800)
    a = p.parse_args()
    if not 1 <= a.seconds <= 10800:
        p.error('bounded duration required')
    a.output.mkdir(parents=True, exist_ok=False)
    command = ['sudo', '-n', 'timeout', '--signal=INT', str(a.seconds),
               'intel_gpu_top', '-J', '-s', '1000']
    stopped = [False]
    process = None

    def stop(*_):
        if stopped[0]:
            return
        stopped[0] = True
        if process is not None and process.poll() is None:
            # Exact child is this observer's sudo/timeout, never a CV worker.
            subprocess.run(['sudo', '-n', 'kill', '-INT', str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    parser = Objects()
    count = 0
    with (a.output/'stderr.log').open('wb') as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                   text=True, bufsize=1)
        (a.output/'launch.json').write_text(json.dumps({
            'command': command, 'child_pid': process.pid,
            'started_utc': datetime.now(timezone.utc).isoformat(),
            'started_monotonic': time.monotonic(), 'scope': __doc__}, indent=2))
        try:
            with gzip.open(a.output/'raw.stdout.gz', 'xt') as raw, gzip.open(a.output/'samples.jsonl.gz', 'xt') as out:
                for line in process.stdout:
                    raw.write(line)
                    for sample in parser.feed(line):
                        received = time.monotonic()
                        out.write(json.dumps({'received_utc': datetime.now(timezone.utc).isoformat(),
                            'received_monotonic': received,
                            'scope': 'Node GPU engines, power and raw client counters; not isolated CV utilization',
                            'timestamp_semantics': 'Receipt of completed tool sample; hardware interval in period.duration, not kernel timestamp',
                            'sample': sample}, separators=(',', ':'))+'\n')
                        count += 1
                        out.flush()
                        raw.flush()
            result = process.wait(timeout=10)
        finally:
            stop()
        (a.output/'finished.json').write_text(json.dumps({
            'samples': count, 'exit_code': result, 'partial_json_bytes': len(parser.pending),
            'ended_utc': datetime.now(timezone.utc).isoformat(),
            'scope': 'Observation ended, not qualification'}, indent=2))


if __name__ == '__main__':
    main()
