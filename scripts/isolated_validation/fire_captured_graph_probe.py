#!/usr/bin/env python3
"""Replay an observed native fire input through its exact pre-compile graph.

Explicit CPU numerical reference and GPU diagnostics only. No production device
fallback, modified weights, changed gates, or capacity/accuracy qualification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np
import openvino as ov


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(model, data, device, fp32):
    core = ov.Core()
    config = {'INFERENCE_NUM_THREADS': 1} if device == 'CPU' else {
        'COMPILATION_NUM_THREADS': 1, 'NUM_STREAMS': 'AUTO'}
    if fp32:
        config['INFERENCE_PRECISION_HINT'] = 'f32'
    compiled = core.compile_model(str(model), device, config)
    result = np.asarray(compiled([data])[0]).copy()
    assert result.shape == (1, 6, 2100) and np.isfinite(result).all()
    properties = {}
    for key in ['INFERENCE_PRECISION_HINT', 'LP_TRANSFORMS_MODE', 'EXECUTION_DEVICES']:
        try:
            properties[key] = str(compiled.get_property(key))
        except RuntimeError:
            properties[key] = 'unavailable'
    return result, {'device':device, 'config':config, 'properties':properties,
                    'fire_max':float(result[0, 4].max()), 'smoke_max':float(result[0, 5].max())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--reference', type=Path, default=Path('/models/traffic/openvino/smoke_fire.xml'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = json.loads(args.metadata.read_text())
    assert metadata['element_type'] == 'u8' and metadata['shape'] == [1, 3, 320, 320]
    assert metadata['strides'] == [307200, 102400, 320, 1]
    data = np.fromfile(args.input, dtype=np.uint8).reshape(metadata['shape'])
    assert data.nbytes == metadata['bytes'] and not data.any(), 'This first diagnostic uses verified black only'
    report = {'scope':__doc__, 'openvino_version':ov.__version__, 'started_unix':time.time(),
              'files_sha256':{str(p):sha(p) for p in [args.model, args.model.with_suffix('.bin'),
                 args.reference, args.reference.with_suffix('.bin'), args.input, args.metadata]},
              'captured_input':metadata, 'runs':[]}
    for model in [args.model, args.reference]:
        counts = {}
        for layer in ET.parse(model).findall('./layers/layer'):
            name = layer.attrib['type']; counts[name] = counts.get(name, 0) + 1
        report.setdefault('operation_inventory', {})[str(model)] = counts
    def save():
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    save()
    try:
        for device, fp32 in [('CPU',True), ('GPU',False), ('GPU',True)]:
            outputs = []
            for label, model, input_data in [('captured_native_graph',args.model,data),
                    ('original_float_graph',args.reference,data.astype(np.float32)/255.)]:
                output, row = run(model, input_data, device, fp32)
                np.save(args.output/(label+'-'+device+'-'+str(fp32)+'.npy'), output)
                row['graph'] = label; report['runs'].append(row); outputs.append(output); save()
            difference = np.abs(outputs[0]-outputs[1])
            report.setdefault('same_black_comparisons', []).append({'device':device,'fp32':fp32,
                'maximum_absolute_difference':float(difference.max()),
                'smoke_max_difference':abs(float(outputs[0][0,5].max())-float(outputs[1][0,5].max()))})
            save()
        report['execution_complete'] = True
    except Exception as error:
        report.update(execution_complete=False,error=type(error).__name__+': '+str(error))
        raise
    finally:
        report['ended_unix'] = time.time(); save()


if __name__ == '__main__':
    main()
