#!/usr/bin/env python3
"""Test-only looped MPEG-TS source; accept a new consumer after disconnect.

Each connection begins a new file epoch. This is not a capture-clock or marker
latency source. It avoids a dead single-listen FFmpeg after CV worker recovery.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from mpegts_pacing import FramePacer, frame_groups


def stop_child(child):
    if child is not None:
        if child.stdout is not None:
            child.stdout.close()
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def serve(fixture, port, frame_log=None):
    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', port))
        server.listen(2)
        epoch = 0
        while True:
            connection, _ = server.accept()
            epoch += 1
            child = None
            frames = None
            try:
                if frame_log:
                    frames = os.fdopen(os.open(frame_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND,0o600), 'w',buffering=1)
                print(json.dumps({'event': 'source_connected', 'epoch': epoch,
                                  'monotonic': time.monotonic(), 'port': port}), flush=True)
                with connection:
                    child = subprocess.Popen([
                        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
                        '-stream_loop', '-1', '-i', str(fixture), '-map', '0:v:0',
                        '-an', '-c:v', 'copy', '-f', 'mpegts', 'pipe:1',
                    ], stdout=subprocess.PIPE)
                    pacer = FramePacer()
                    for index, ((pts, dts), data) in enumerate(frame_groups(child.stdout)):
                        pacer.pace(dts)
                        submitted = time.monotonic()
                        connection.sendall(data)
                        if frames:
                            frames.write(json.dumps({'epoch':epoch,'producer_pid':os.getpid(),
                                'decode_order_index':index,'pts90k':pts,'dts90k':dts,
                                'first_bytes_submitted_mono':submitted,
                                'all_bytes_submitted_mono':time.monotonic(),
                                'transport_bytes':len(data)})+'\n')
                    result = child.wait()
                    if result:
                        raise RuntimeError('FFmpeg replay source failed')
                print(json.dumps({'event': 'source_disconnected', 'epoch': epoch,
                                  'exit_code': result, 'monotonic': time.monotonic(),
                                  'port': port}), flush=True)
            except (BrokenPipeError, ConnectionResetError):
                print(json.dumps({'event': 'source_disconnected', 'epoch': epoch,
                                  'monotonic': time.monotonic(), 'port': port}), flush=True)
            finally:
                connection.close()
                stop_child(child)
                if frames:frames.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--frame-log',type=Path)
    args = parser.parse_args()
    if not args.fixture.is_file() or not 0 < args.port < 65536:
        parser.error('readable fixture and valid TCP port required')
    serve(args.fixture, args.port,args.frame_log)
