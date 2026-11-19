#!/usr/bin/env python3
"""Test-only independent TCP sources on a separate host; no inference or encoding.

Uses the existing paced sender with a fresh log directory for each connection.
PES indices alone are not visible-frame latency oracles on unmarked recordings.
"""
import argparse
from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bind', required=True)
    p.add_argument('--peer', required=True)
    p.add_argument('--count', type=int, required=True)
    p.add_argument('--port-base', type=int, default=19101)
    a = p.parse_args()
    if not a.fixture.is_file() or not 1 <= a.count <= 50:
        p.error('existing fixture and 1..50 sources required')
    ipaddress.ip_address(a.bind)
    ipaddress.ip_address(a.peer)
    if not 1 <= a.port_base <= 65536-a.count:
        p.error('port range exceeds TCP bounds')
    reservations = []
    try:
        for index in range(a.count):
            s = socket.socket()
            reservations.append(s)
            s.bind((a.bind, a.port_base+index))
    finally:
        for s in reservations:
            s.close()
    a.output.mkdir(parents=True, exist_ok=False)
    stop = threading.Event()
    for s in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(s, lambda *_: stop.set())

    def serve(index):
        port = a.port_base + index
        epoch = 0
        while not stop.is_set() and not (a.output/'STOP').exists():
            epoch += 1
            directory = a.output/str(port)/str(epoch)
            directory.mkdir(parents=True)
            command = [sys.executable, str(Path(__file__).with_name('marker_sender.py')),
                       '--port', str(port), '--fixture', str(a.fixture), '--logdir', str(directory),
                       '--bind', a.bind, '--peer', a.peer]
            with (directory/'source.log').open('w') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
                try:
                    while child.poll() is None:
                        if stop.wait(.5) or (a.output/'STOP').exists():
                            break
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
            (directory/'result.json').write_text(json.dumps({
                'utc': datetime.now(timezone.utc).isoformat(), 'returncode': child.returncode,
                'port': port, 'epoch': epoch})+'\n')
            stop.wait(.5)

    (a.output/'manifest.json').write_text(json.dumps({
        'pid': os.getpid(), 'fixture': str(a.fixture.resolve()),
        'urls': [f'tcp://{a.bind}:{a.port_base+i}' for i in range(a.count)],
        'peer': a.peer, 'scope': __doc__}, indent=2)+'\n')
    threads = [threading.Thread(target=serve, args=(i,)) for i in range(a.count)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads) and not stop.wait(1):
            if (a.output/'STOP').exists():
                break
    finally:
        stop.set()
        for t in threads:
            t.join()


if __name__ == '__main__':
    main()
