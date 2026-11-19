#!/usr/bin/env python3
"""Compare frozen images on identical pixels; diagnostic, never capacity evidence.

Uses the baked detector wrapper and baked DL Streamer scene executor. Decoding
the test recording here is fixture preparation, not the production decode path.
Every input image, returned detection and vector is retained. No runtime source
or model is mounted into either image.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', required=True)
    p.add_argument('--candidate', required=True)
    p.add_argument('--video', action='append', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--compiler-cpus', default='6,7',
                   help='Explicit host validation affinity, never a capacity setting')
    p.add_argument('--detector-only', action='store_true',
                   help='Keep detector numerical qualification separate from native scene witness tests')
    a = p.parse_args()
    import cv2
    import numpy as np
    root = Path(__file__).resolve().parents[2]
    a.output.mkdir(parents=True, exist_ok=False)
    out = a.output.resolve()
    images = out / 'images'
    images.mkdir()
    frames = []
    for index, video in enumerate(a.video):
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise RuntimeError('fixture video unavailable: ' + str(video))
        for n in range(1, 121):
            ok, frame = cap.read()
            if not ok:
                break
            if n in (1, 50, 100):
                name = f'{index}-{n:03d}.jpg'
                assert cv2.imwrite(str(images / name), frame)
                frames.append({'name': name, 'video': str(video), 'frame': n,
                               'sha256': hashlib.sha256((images/name).read_bytes()).hexdigest()})
        cap.release()
    if len(frames) < 3:
        raise RuntimeError('insufficient fixture samples')
    (out / 'inputs.json').write_text(json.dumps(frames, indent=2))
    # Existing probe, explicit QAFT path: its historical default is another model.
    detector = root / 'scripts/v8_detector_probe.py'
    scene = root / 'optimization_evidence/2026-11-10-local-v8-r2/fixtures/scene_device_probe.py'
    spec = importlib.util.spec_from_file_location('comparison', root/'scripts/v8_parity_harness.py')
    comparison = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comparison)
    images_by_role = {}
    # This is numerical qualification, not a speed benchmark. Confine cold
    # compiler CPU work to an explicit efficiency-core set to avoid turbo spikes.
    compiler_cpus = {int(x) for x in a.compiler_cpus.split(',')}
    if not compiler_cpus.issubset(os.sched_getaffinity(0)):
        raise RuntimeError('declared validation CPU set is unavailable')
    prefix = f'import os; os.sched_setaffinity(0, {compiler_cpus!r}); '
    # GPU compilation can create its own CPU worker pool. A CPU quota alone
    # permits short all-core bursts. Limit compiler concurrency identically in
    # both numerical paths, without altering model data or GPU execution.
    setup = '''
from detectors.backends.openvino_detector import shared_core
shared_core().set_property('GPU', {'COMPILATION_NUM_THREADS':1,'ENABLE_CPU_PINNING':False})
import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst
Gst.init([])
_parse = Gst.parse_launch
def _bounded_compilation(description):
    if 'gvainference ' in description:
        description = description.replace('gvainference ', 'gvainference ie-config="COMPILATION_NUM_THREADS=1,ENABLE_CPU_PINNING=NO" ', 1)
    return _parse(description)
Gst.parse_launch = _bounded_compilation
'''
    script_runner = prefix + setup + ('\nimport sys,runpy; sys.argv=sys.argv[1:]; '
                              'runpy.run_path(sys.argv[0],run_name="__main__")')
    for role, image in [('baseline', a.baseline), ('candidate', a.candidate)]:
        inspect = subprocess.check_output(['podman', 'image', 'inspect', image], text=True)
        (out/f'{role}-image.json').write_text(inspect)
        image_info = json.loads(inspect)[0]
        image_id = image_info['Id']
        image_env = dict(x.split('=', 1) for x in image_info['Config']['Env'])
        python_path = '/opt/traffic-pilot/services/worker:' + image_env.get('PYTHONPATH', '')
        images_by_role[role] = image_id
        target = out / role
        target.mkdir()
        scratch = target / 'private-tmp'
        scratch.mkdir()
        base = ['podman', 'run', '--rm', '--network', 'none',
                '--cpu-period', '10000', '--cpu-quota', '10000',
                # Root in the default rootless namespace maps to the invoking
                # host user. keep-id can clone gigabytes of image layers.
                # This numerical probe does not validate deployment UID.
                '--user', '0:0',
                '--group-add', 'keep-groups', '--device', '/dev/dri',
                '--device', '/dev/accel', '-v', f'{scratch}:/tmp',
                '-e', 'TMPDIR=/tmp', '-e',
                'PYTHONPATH=' + python_path,
                '-v', f'{target}:/results', '--entrypoint', 'python3']
        labels = []
        for variable, label in [('SENTINEL_VALIDATION_RUN_ID', 'sentinel.validation_run'),
                                ('SENTINEL_VALIDATION_CASE', 'sentinel.validation_case')]:
            if os.getenv(variable):
                labels += ['--label', f'{label}={os.environ[variable]}']
        base[2:2] = labels
        preflight = subprocess.run(base + [image_id, '-c',
                prefix + 'import gstgva; from pipeline.dls_secondary import DlsSceneExtractor; '
                'from detectors.backends.openvino_detector import OpenVINOYOLODetector; '
                'print("baked SDK imports OK", sorted(os.sched_getaffinity(0)))'],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        (target/'preflight.log').write_text(preflight.stdout)
        preflight.check_returncode()
        for index, video in enumerate(a.video):
            cmd = base[:-2] + ['-v', f'{detector}:/checks/probe.py:ro',
                    '-v', f'{video.resolve()}:/input.mp4:ro'] + base[-2:] + [
                    image_id, '-c', script_runner, '/checks/probe.py', '--video', '/input.mp4',
                    '--output', f'/results/detector-{index}.json',
                    '--model', '/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml',
                    '--maximum-samples', '20', '--sample-every', '5']
            print(json.dumps({'role': role, 'stage': 'detector', 'video': index}), flush=True)
            result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=300)
            (target/f'detector-{index}.log').write_text(result.stdout)
            result.check_returncode()
            result_data = json.loads((target/f'detector-{index}.json').read_text())
            assert result_data['device_requested'] == 'GPU'
            assert all(x.startswith('GPU') for x in result_data['execution_devices'].split(','))
        if a.detector_only:
            continue
        cmd = base[:-2] + ['-v', f'{images}:/images:ro',
                '-v', f'{scene}:/checks/scene.py:ro'] + base[-2:] + [
                image_id, '-c', script_runner, '/checks/scene.py', 'GPU']
        print(json.dumps({'role': role, 'stage': 'scene_gpu'}), flush=True)
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=300)
        (target/'scene.log').write_text(result.stdout)
        result.check_returncode()
    for i in range(len(a.video)):
        left = json.loads((out/f'baseline/detector-{i}.json').read_text())
        right = json.loads((out/f'candidate/detector-{i}.json').read_text())
        assert left['model_sha256'] == right['model_sha256']
        assert [(x['frame_index'], x['input_bgr_sha256']) for x in left['frames']] == [
               (x['frame_index'], x['input_bgr_sha256']) for x in right['frames']]
    detections = [comparison.compare_detections(out/f'baseline/detector-{i}.json',
                   out/f'candidate/detector-{i}.json') for i in range(len(a.video))]
    scene_result = {'tested': False, 'reason': 'Separate original-native-context scene witness test'}
    if not a.detector_only:
        old = np.load(out/'baseline/GPU.npz')['vectors'].astype(np.float64)
        new = np.load(out/'candidate/GPU.npz')['vectors'].astype(np.float64)
        assert old.shape == new.shape and np.isfinite(old).all() and np.isfinite(new).all()
        cos = np.sum(old*new, axis=1)/(np.linalg.norm(old, axis=1)*np.linalg.norm(new, axis=1))
        scene_result = {'tested': True, 'shape': list(old.shape), 'cosine_each': cos.tolist(),
                       'absolute_delta_max': float(np.max(np.abs(old-new))),
                       'l2_delta_each': np.linalg.norm(old-new, axis=1).tolist(),
                       'exact_equal': bool(np.array_equal(old, new))}
    report = {'utc': datetime.now(timezone.utc).isoformat(), 'images': images_by_role,
              'validation_cpu_affinity': sorted(compiler_cpus),
              'validation_compilation_threads': 1, 'validation_cpu_quota': '10000/10000',
              'validation_gpu_cpu_pinning': False,
              'detector': detections, 'scene': scene_result,
              'all_detector_strict_parity': all(x['strict_parity'] for x in detections),
              'scope': 'Same pixels through baked GPU model wrappers; not native VA preprocessing, tracking/event parity or capacity'}
    (out/'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return 0 if report['all_detector_strict_parity'] else 1


if __name__ == '__main__':
    sys.exit(main())
