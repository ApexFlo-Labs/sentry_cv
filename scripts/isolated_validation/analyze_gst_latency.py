#!/usr/bin/env python3
"""Native element wall times including waits/preprocessing; not kernel time.

Distributions include traced startup/warmup/steady frames, because tracer receipt
timestamps alone do not prove alignment to the harness's steady clock interval.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re


def stats(values):
    values = sorted(values)
    def percentile(q):
        index = q*(len(values)-1)
        left = int(index)
        return values[left]+(values[min(left+1,len(values)-1)]-values[left])*(index-left)
    return {'samples': len(values), 'min_ms': values[0], 'p50_ms': percentile(.5),
            'p95_ms': percentile(.95), 'max_ms': values[-1]}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    values = defaultdict(list)
    files = []
    for source in a.root.rglob('gst-latency.log*'):
        if not source.is_file():
            continue
        files.append(str(source))
        with source.open(errors='replace') as stream:
            for line in stream:
                if 'element-latency,' not in line:
                    continue
                fields = dict(re.findall(r'([\w-]+)=\((?:string|guint64)\)([^,;]+)', line))
                if not fields.get('time', '').isdigit() or 'element' not in fields:
                    continue
                name = fields['element'].strip('"')
                key = name+'@'+fields.get('element-id','unknown').strip('"')
                values[key].append(int(fields['time'])/1e6)
    critical = {name: sum(len(v) for k,v in values.items() if k.split('@')[0] == name)
                for name in ['primarydet','facedet','faceclass']}
    result = {'scope': __doc__, 'files': files,
              'passed': bool(values and all(critical.values())), 'critical_samples': critical,
              'notes': ['Element IDs distinguish different instances with the same name.',
                        'These are per-buffer element residence times; inference-interval bypasses and frames without eligible ROIs are included, not separate model calls.',
                        'Do not sum overlapping branch times or call these pure GPU/NPU execution times.',
                        'Generic secondary element names are not assigned to a model without independent identity evidence.'],
              'elements': {k:stats(v) for k,v in values.items()}}
    a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed': result['passed'], 'files':len(files), 'critical_samples':critical}))
    raise SystemExit(0 if result['passed'] else 1)
