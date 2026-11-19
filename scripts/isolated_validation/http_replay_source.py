#!/usr/bin/env python3
"""Bounded local HTTP MPEG-TS replay for native input/reconnect qualification.

Each probe/decoder gets a separate paced compressed stream. No re-encoding,
inference, authentication changes or external server. Optional fixture TLS.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import ssl
import subprocess
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--seconds', type=int, default=1200)
    p.add_argument('--certificate', type=Path)
    p.add_argument('--key', type=Path)
    args = p.parse_args()
    if not args.fixture.is_file() or not 1 <= args.seconds <= 7200:
        p.error('existing fixture and bounded duration required')
    if bool(args.certificate) != bool(args.key):
        p.error('both TLS fixture files required')
    args.output.mkdir(parents=True, exist_ok=False)
    stopped = threading.Event()
    children, handlers, lock = set(), [], threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != '/live.ts':
                self.send_error(404)
                return
            with lock:
                index = len(handlers)+1
                record = {'connection': index, 'started_utc': datetime.now(timezone.utc).isoformat(),
                          'bytes_written': 0}
                handlers.append(record)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Connection', 'close')
            self.end_headers()
            command = ['ffmpeg', '-v', 'error', '-re', '-stream_loop', '-1',
                       '-i', str(args.fixture), '-map', '0:v:0', '-an', '-c:v', 'copy',
                       '-f', 'mpegts', 'pipe:1']
            with (args.output/f'connection-{index:03d}.stderr.log').open('wb') as errors:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
                with lock:
                    children.add(child)
                try:
                    while not stopped.is_set():
                        data = child.stdout.read1(65536)
                        if not data:
                            break
                        self.wfile.write(data)
                        self.wfile.flush()
                        record['bytes_written'] += len(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    child.stdout.close()
                    with lock:
                        children.discard(child)
                    record.update(ended_utc=datetime.now(timezone.utc).isoformat(), exit_code=child.returncode)
                    (args.output/f'connection-{index:03d}.json').write_text(json.dumps(record, indent=2)+'\n')

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    if args.certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.certificate, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stopped.wait(args.seconds)
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        with lock:
            for child in list(children):
                child.terminate()
        (args.output/'finished.json').write_text(json.dumps({
            'utc': datetime.now(timezone.utc).isoformat(), 'connections': handlers,
            'scope': __doc__}, indent=2)+'\n')


if __name__ == '__main__':
    main()
