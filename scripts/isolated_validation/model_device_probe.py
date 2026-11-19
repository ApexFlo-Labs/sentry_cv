#!/usr/bin/env python3
"""One baked model/device in isolation. Synthetic input; no CV policy/capacity claim."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import numpy as np
import openvino as ov

MODELS = {
    'pose': '/models/face/openvino/best_a0_fp16.xml',
    'face': '/models/face/openvino/dlstreamer/face_align_u8.xml',
    'body': '/models/surveillance/transreid_ssl_int8.xml',
    'gait': '/models/surveillance/gaitbase_dlstreamer_montage.xml',
    'scene': '/models/scene/openvino/siglip2-base-patch16-224-static.xml',
    'plate': '/models/traffic/openvino/license_plate.xml',
    'ocr': '/models/traffic/openvino/ocr.xml',
    'fire': '/models/traffic/openvino/smoke_fire.xml',
    'primary': '/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml',
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', choices=MODELS, required=True)
    p.add_argument('--device', choices=['GPU', 'NPU'], required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    log = (a.output/'stages.jsonl').open('x', buffering=1)

    def stage(event, **fields):
        row = dict(utc=datetime.now(timezone.utc).isoformat(), monotonic=time.monotonic(),
                   event=event, model=a.model, device=a.device, **fields)
        text = json.dumps(row)
        log.write(text+'\n')
        print(text, flush=True)

    report = {'scope': __doc__, 'model': a.model, 'path': MODELS[a.model],
              'requested_device': a.device, 'execution_completed': False}
    try:
        stage('core_begin', allowed_cpus=sorted(os.sched_getaffinity(0)))
        core = ov.Core()
        stage('read_begin', available_devices=core.available_devices)
        model = core.read_model(MODELS[a.model])
        inputs = []
        signature = []
        for port in model.inputs:
            shape = [int(n) for n in port.shape]
            dtype = port.element_type.to_dtype()
            inputs.append(np.zeros(shape, dtype=dtype))
            signature.append({'shape': shape, 'dtype': str(np.dtype(dtype))})
        stage('compile_begin', inputs=signature)
        began = time.monotonic()
        compiled = core.compile_model(model, a.device)
        report['compile_seconds'] = time.monotonic()-began
        raw_devices = compiled.get_property('EXECUTION_DEVICES')
        devices = [raw_devices] if isinstance(raw_devices, str) else list(raw_devices)
        devices = [str(d) for d in devices]
        report['execution_devices'] = devices
        stage('execution_devices', values=devices, property_type=type(raw_devices).__name__)
        if any('CPU' in d for d in devices) or not any(a.device in d for d in devices):
            raise RuntimeError('Unexpected execution device; fallback is forbidden')
        stage('compile_complete', seconds=report['compile_seconds'], execution_devices=devices)
        report.update(execution_devices=devices, input_signature=signature, iterations=[])
        request = compiled.create_infer_request()
        for index in range(3):
            stage('infer_begin', iteration=index)
            began = time.monotonic()
            outputs = request.infer(inputs)
            elapsed = time.monotonic()-began
            row = {'iteration': index, 'call_seconds': elapsed,
                   'outputs': [{'shape': list(v.shape), 'finite': bool(np.isfinite(v).all())}
                               for v in outputs.values()]}
            report['iterations'].append(row)
            stage('infer_complete', **row)
        report['execution_completed'] = True
        stage('finished', execution_completed=True)
    except Exception as e:
        report['error'] = type(e).__name__+': '+str(e)
        stage('failed', error=report['error'])
    finally:
        (a.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        log.close()
    return 0 if report['execution_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
