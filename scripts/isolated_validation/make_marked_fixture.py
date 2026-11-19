#!/usr/bin/env python3
"""Offline, explicit marker fixture creation; preserves source geometry/rate.

The bottom 24 pixels contain a checksum-protected frame index. This changes
test pixels and re-encodes the source, so it is a separate workload, not an
accuracy/parity reference. Never run the encoder during a measured CV window.
"""
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def paint(frame, index):
    h, w = frame.shape[:2]
    if w < 688 or h < 24:
        raise ValueError('marker needs at least688x24 source pixels')
    bits = f'{0xA55A:016b}{index:032b}{sum(index.to_bytes(4, "big")) & 255:08b}'
    frame[h-26:h-2, 4:688] = 127
    for bit, value in enumerate(bits):
        frame[h-24:h-4, 8+12*bit:8+12*(bit+1)] = 255 if value == '1' else 0
    return frame


def main():
    import cv2
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--seconds', type=int, default=120)
    p.add_argument('--pace-fps', type=float, default=20,
                   help='Offline CPU-load pacing only; encoded source FPS is unchanged')
    a = p.parse_args()
    if a.seconds < 1 or a.pace_fps <= 0 or a.output.exists() or a.output.with_suffix('.json').exists():
        p.error('positive duration and a fresh output/sidecar path required')
    info = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=width,height,avg_frame_rate', '-of', 'json', str(a.source)]))['streams'][0]
    w, h = info['width'], info['height']
    rate = Fraction(info['avg_frame_rate'])
    if w < 688 or h < 26 or rate <= 0:
        raise ValueError('unsupported marked fixture geometry/rate')
    frames = int(rate * a.seconds)
    cv2.setNumThreads(1)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    phase = os.environ.get('SENTINEL_VALIDATION_PHASE_FILE')
    if phase:
        Path(phase).write_text(json.dumps({'phase':'startup', 'scope':'Offline fixture only; no performance window'}))
    cap = cv2.VideoCapture(str(a.source))
    cmd = ['ffmpeg', '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'bgr24',
           '-video_size', f'{w}x{h}', '-framerate', str(rate), '-i', 'pipe:0', '-an',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast', '-crf', '20',
           '-threads', '2', '-g', str(round(float(rate))), '-bf', '0', '-n', str(a.output)]
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for index in range(frames):
            iteration = time.monotonic()
            if shutil.disk_usage(a.output.parent).free < 2 * 1024**3:
                raise RuntimeError('fixture disk reserve reached')
            ok, image = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, image = cap.read()
            if not ok or image.shape[:2] != (h, w):
                raise RuntimeError('source cannot supply an unchanged-size frame')
            child.stdin.write(paint(image, index).tobytes())
            time.sleep(max(0, 1/a.pace_fps - (time.monotonic()-iteration)))
    finally:
        child.stdin.close()
        cap.release()
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    if child.returncode:
        raise RuntimeError('fixture encoder failed')
    sidecar = dict(source=str(a.source), output=str(a.output), width=w, height=h,
                   source_rate=str(rate), frames=frames, seconds=a.seconds,
                   marker_y=h-14, marker_x0=14, marker_stride=12,
                   scope='Test pixels/re-encode only; source geometry/rate preserved', command=cmd)
    for name, path in [('source_sha256', a.source), ('output_sha256', a.output)]:
        with path.open('rb') as f:
            sidecar[name] = hashlib.file_digest(f, 'sha256').hexdigest()
    a.output.with_suffix('.json').write_text(json.dumps(sidecar, indent=2)+'\n')
    print(a.output)


if __name__ == '__main__':
    main()
