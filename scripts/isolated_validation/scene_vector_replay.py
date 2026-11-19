#!/usr/bin/env python3
"""Replay lossless production-path scene witnesses through the baked executor.

Run inside an explicit image, with witness directory mounted read-only. The
reference PNG pixel hash and vector hash must match the capture metadata. This
is numerical embedding-space evidence, never capacity or recognition accuracy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--device', choices=['GPU', 'NPU'], required=True)
    a = p.parse_args()
    import cv2
    import numpy as np
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init([])
    parse = Gst.parse_launch

    def bounded_compilation(description):
        if 'gvainference ' in description and 'device=GPU ' in description:
            description = description.replace('gvainference ',
                'gvainference ie-config="COMPILATION_NUM_THREADS=1,ENABLE_CPU_PINNING=NO" ', 1)
        return parse(description)
    Gst.parse_launch = bounded_compilation
    sys.path.insert(0, '/opt/traffic-pilot/services/worker')
    from pipeline.dls_secondary import DlsSceneExtractor
    samples = sorted(a.input.glob('*.png'))
    if len(samples) < 4:
        raise ValueError('at least four original-pixel scene witnesses required')
    a.output.mkdir(exist_ok=True, parents=True)
    extractor = DlsSceneExtractor(
        '/models/scene/openvino/siglip2-base-patch16-224-static.xml',
        '/opt/traffic-pilot/config/scene_siglip_model_proc.json',
        a.device, owner_id='scene-numeric-replay')
    # Numerical probe only: allow bounded cold compilation without changing
    # the production engine's timeout or making a latency/capacity claim.
    extractor.engine._engine.timeout_seconds = 180
    rows = []
    try:
        extractor.warmup()
        for image in samples:
            metadata = json.loads(image.with_suffix('.json').read_text())
            original = image.with_suffix('.npy')
            assert hashlib.sha256(image.read_bytes()).hexdigest() == metadata['input_png_sha256']
            assert hashlib.sha256(original.read_bytes()).hexdigest() == metadata['vector_sha256']
            pixels = cv2.imread(str(image))
            assert pixels is not None and list(pixels.shape) == metadata['shape']
            assert hashlib.sha256(pixels.tobytes()).hexdigest() == metadata['input_pixel_sha256']
            start = time.monotonic()
            value = extractor.embed(pixels)
            elapsed = time.monotonic() - start
            reference = np.load(original, allow_pickle=False)
            assert value.shape == reference.shape == (768,)
            assert np.isfinite(value).all() and np.isfinite(reference).all()
            old, new = reference.astype(np.float64), value.astype(np.float64)
            cosine = float(np.dot(old, new)/(np.linalg.norm(old)*np.linalg.norm(new)))
            delta = float(np.linalg.norm(old-new))
            np.save(a.output/(image.stem+'.npy'), value)
            row = {'input': image.name, 'pixel_sha256': metadata['input_pixel_sha256'],
                   'reference_device': metadata['actual_device'],
                   'candidate_device': extractor.actual_device,
                   'cosine': cosine, 'l2_delta': delta,
                   'component_abs_delta_max': float(np.max(np.abs(old-new))),
                   'candidate_norm': float(np.linalg.norm(new)),
                   'reference_norm': float(np.linalg.norm(old)),
                   'exact_equal': bool(np.array_equal(reference, value)),
                   'wall_seconds_diagnostic_only': elapsed}
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        extractor.close()
    passed = all(x['cosine'] >= .9999 and x['l2_delta'] <= .015
                 and abs(x['candidate_norm']-1) <= .01 for x in rows)
    result = {'passed': passed, 'samples': rows,
              'numeric_test_tolerances': {'minimum_cosine': .9999, 'maximum_l2_delta': .015,
                                          'maximum_unit_norm_error': .01},
              'cold_probe_timeout_seconds': 180, 'gpu_compilation_threads': 1,
              'scope': __doc__}
    (a.output/'report.json').write_text(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
