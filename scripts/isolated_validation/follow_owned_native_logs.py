#!/usr/bin/env python3
"""Follow new containers belonging to one validation run across sequential cases."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=10800)
    a = p.parse_args()
    stopped = threading.Event()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: stopped.set())
    seen, children = set(), []
    deadline = time.monotonic()+a.seconds
    with a.output.open('a', buffering=1) as log:
        try:
            while not stopped.is_set() and time.monotonic() < deadline:
                if (a.root/'STOP').exists():
                    break
                result = subprocess.run(['docker','ps','-q','--filter',
                    'label=sentinel.validation_run='+a.run_id], capture_output=True, text=True, timeout=5)
                result.check_returncode()
                for cid in result.stdout.split():
                    if cid in seen:
                        continue
                    record = json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]
                    labels = record['Config']['Labels']
                    if labels.get('sentinel.validation_run') != a.run_id or not labels.get('sentinel.validation_case'):
                        raise RuntimeError('Ownership labels missing')
                    child = subprocess.Popen(['docker','logs','--follow','--timestamps',cid],
                                             stdout=log, stderr=subprocess.STDOUT)
                    children.append(child)
                    seen.add(cid)
                stopped.wait(1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    main()
