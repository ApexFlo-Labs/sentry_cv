#!/usr/bin/env python3
"""Renew bounded read-only observers across a long validation campaign."""
import argparse
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=43200)
    p.add_argument('--output-name', default='observations',
                   help='Fresh segment directory; never overwrites prior measurements')
    a = p.parse_args()
    assert re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}', a.output_name)
    r = a.root.resolve()
    stop = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stop.set())
    commands = {
        'host': ['scripts/isolated_validation/campaign_observer.py','--root','{output}',
                 '--current-file',str(r/'CURRENT.json'),'--port','18380','--seconds','10800'],
        'gpu': ['scripts/isolated_validation/gpu_timestamp_observer.py','--output','{output}',
                '--seconds','10800'],
        'process': ['scripts/isolated_validation/process_load_observer.py','--output','{output}.jsonl.gz',
                    '--seconds','10800'],
    }
    children, epochs, starts = {}, {}, {}
    env = dict(os.environ)
    env['PATH'] = '/home/admin1/sentry_cv/.venv/bin:'+env['PATH']
    destination = r/'artifacts'/a.output_name
    destination.mkdir(exist_ok=False)
    deadline = time.monotonic()+a.seconds
    try:
        while not stop.is_set() and time.monotonic()<deadline and not (r/'STOP').exists():
            for name, args in commands.items():
                child = children.get(name)
                if child is not None and child.poll() is None:
                    continue
                # Do not rapidly respawn a broken sampler.
                if time.monotonic()-starts.get(name,-1000)<60:
                    continue
                epoch = epochs.get(name,0)+1
                epochs[name] = epoch
                output = destination/(name+'-'+str(epoch))
                command = ['taskset','-c','14,15','python3']+[
                    value.replace('{output}',str(output)) for value in args]
                with (destination/(name+'-'+str(epoch)+'.log')).open('w') as log:
                    child = subprocess.Popen(command,cwd=r/'repo',env=env,
                        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                children[name] = child
                starts[name] = time.monotonic()
                (destination/(name+'-'+str(epoch)+'-launch.json')).write_text(json.dumps({
                    'utc':datetime.now(timezone.utc).isoformat(),'pid':child.pid,'command':command,
                    'scope':'Read-only sampler; no pipeline/power/affinity changes to CV'},indent=2)+'\n')
            stop.wait(5)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()


if __name__ == '__main__':
    main()
