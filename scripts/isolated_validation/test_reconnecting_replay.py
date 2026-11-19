"""Receiver disconnect and process cleanup regressions for the TEST source."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import importlib.util


def test_two_consumers_reconnect_and_sender_reaps_child(tmp_path):
    fixture = tmp_path / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=size=64x64:rate=5:duration=1', '-threads', '1',
                    '-c:v', 'libx264', '-y', str(fixture)], check=True)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    script = Path(__file__).with_name('reconnecting_replay.py')
    log = tmp_path / 'source.log'
    with log.open('w') as output:
        producer = subprocess.Popen([sys.executable, str(script), '--fixture',
                                     str(fixture), '--port', str(port)],
                                    stdout=output, stderr=output)
        child_pid = None
        try:
            for epoch in range(2):
                deadline = time.monotonic() + 8
                while True:
                    try:
                        consumer = socket.create_connection(('127.0.0.1', port), timeout=2)
                        break
                    except OSError:
                        assert producer.poll() is None and time.monotonic() < deadline
                        time.sleep(.05)
                with consumer:
                    data = consumer.recv(188 * 20)
                    assert data and data[0] == 0x47
                    assert producer.poll() is None
                    children = Path(f'/proc/{producer.pid}/task/{producer.pid}/children').read_text().split()
                    assert len(children) == 1
                    child_pid = int(children[0])
                if epoch == 0:
                    while f'"event": "source_disconnected"' not in log.read_text():
                        assert time.monotonic() < deadline
                        time.sleep(.05)
            producer.send_signal(signal.SIGTERM)
            assert producer.wait(timeout=5) == 0
            assert not Path(f'/proc/{child_pid}').exists()
        finally:
            if producer.poll() is None:
                producer.terminate()
                producer.wait(timeout=5)
            if child_pid and Path(f'/proc/{child_pid}').exists():
                os.kill(child_pid, signal.SIGKILL)
    events = [json.loads(row) for row in log.read_text().splitlines() if row.startswith('{')]
    assert [e['epoch'] for e in events if e['event'] == 'source_connected'] == [1, 2]


def test_live_replay_does_not_burst_after_source_process_suspension(tmp_path):
    fixture=tmp_path/'fixture.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                    'color=size=64x64:rate=10:duration=2','-threads','1',
                    '-c:v','libx264','-y',str(fixture)],check=True)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    script=Path(__file__).with_name('reconnecting_replay.py')
    spec=importlib.util.spec_from_file_location('pacer_test',script.with_name('mpegts_pacing.py'))
    pacing=importlib.util.module_from_spec(spec);spec.loader.exec_module(pacing)
    producer=subprocess.Popen([sys.executable,str(script),'--fixture',str(fixture),
                               '--port',str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    consumer=None
    try:
        deadline=time.monotonic()+5
        while consumer is None:
            try:consumer=socket.create_connection(('127.0.0.1',port),timeout=2)
            except OSError:
                assert producer.poll() is None and time.monotonic()<deadline
                time.sleep(.02)
        class Receiver:
            def read(self,n):return consumer.recv(n)
        groups=pacing.frame_groups(Receiver())
        for _ in range(3):next(groups)
        os.kill(producer.pid,signal.SIGSTOP)
        time.sleep(.5)
        os.kill(producer.pid,signal.SIGCONT)
        times=[]
        for _ in range(6):
            next(groups);times.append(time.monotonic())
        assert all(b-a>=.075 for a,b in zip(times,times[1:])), times
    finally:
        if consumer:consumer.close()
        if producer.poll() is None:
            producer.send_signal(signal.SIGCONT)
            producer.terminate()
        producer.wait(timeout=5)
