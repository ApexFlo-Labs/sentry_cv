#!/usr/bin/env python3
"""Bounded multi-consumer TCP MPEG-TS test source, compressed copy only.

Each accepted connection gets an independent paced replay. This is source
fixture work, never part of CV inference or a diverse physical-camera dataset.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import socketserver
import subprocess
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=1200)
    args = parser.parse_args()
    if not args.fixture.is_file() or not 1 <= args.seconds <= 7200:
        parser.error('existing fixture and bounded duration required')
    args.output.mkdir(exist_ok=False)
    stopped, lock = threading.Event(), threading.Lock()
    children, sockets, connections, handlers = set(), set(), [], set()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with lock:
                if stopped.is_set() or len(sockets) >= 16:
                    return
                sockets.add(self.request)
                handlers.add(threading.current_thread())
                index = len(connections)+1
                record = {'connection': index, 'started_utc': datetime.now(timezone.utc).isoformat(),
                          'bytes_submitted': 0}
                connections.append(record)
            self.request.settimeout(30)
            command = ['ffmpeg','-v','error','-re','-stream_loop','-1','-i',str(args.fixture),
                       '-map','0:v:0','-an','-c:v','copy','-f','mpegts','pipe:1']
            with (args.output/f'connection-{index:03d}.stderr.log').open('wb') as log:
                child = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=log)
                with lock:
                    children.add(child)
                try:
                    while not stopped.is_set():
                        data = child.stdout.read1(65536)
                        if not data:
                            break
                        self.request.sendall(data)
                        record['bytes_submitted'] += len(data)
                except OSError:
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
                        sockets.discard(self.request)
                        children.discard(child)
                    record['ended_utc'] = datetime.now(timezone.utc).isoformat()
                    record['exit_code'] = child.returncode
                    (args.output/f'connection-{index:03d}.json').write_text(json.dumps(record,indent=2)+'\n')
                    with lock:
                        handlers.discard(threading.current_thread())

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(('127.0.0.1',args.port),Handler)
    for sig in [signal.SIGINT,signal.SIGTERM]:
        signal.signal(sig,lambda *_:stopped.set())
    threading.Thread(target=server.serve_forever,daemon=True).start()
    (args.output/'ready.json').write_text(json.dumps({'port':server.server_address[1],
                                                   'scope':__doc__},indent=2)+'\n')
    try:
        stopped.wait(args.seconds)
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        with lock:
            active_sockets, active_children, active_handlers = list(sockets), list(children), list(handlers)
        for stream in active_sockets:
            try:
                stream.shutdown(2)
            except OSError:
                pass
        for child in active_children:
            child.terminate()
        for child in active_children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for handler in active_handlers:
            handler.join(timeout=10)
        assert not any(handler.is_alive() for handler in active_handlers), 'source handler cleanup failed'
        (args.output/'finished.json').write_text(json.dumps({'connections':connections,
                      'scope':'Source connections, not image camera-count qualification'},indent=2)+'\n')


if __name__ == '__main__':
    main()
