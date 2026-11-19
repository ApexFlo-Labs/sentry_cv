#!/usr/bin/env python3
"""Fail closed if the qualified metadata helper differs from its provenance.

Do not rebuild this helper using the host's older GstAnalytics headers. Build
with build_gst_metadata.py in the matching SDK environment, then repeat native
metadata parity. This check runs no compiler, camera, model or GPU workload.
"""
import argparse
import hashlib
import json
from pathlib import Path


def verify(root):
    provenance = json.loads((root / 'build-info/gst_metadata.json').read_text())
    for path, key in [('src/gst_detection_metadata.c', 'source_sha256'),
                      ('lib/libsentinel_gst_metadata.so', 'binary_sha256')]:
        actual = hashlib.sha256((root / path).read_bytes()).hexdigest()
        if actual != provenance.get(key):
            raise ValueError('metadata helper provenance mismatch: ' + path)
    if provenance.get('gst_analytics_version') != '1.28.2':
        raise ValueError('metadata helper requires qualified SDK GstAnalytics 1.28.2')
    return {'source_sha256': provenance['source_sha256'],
            'binary_sha256': provenance['binary_sha256'],
            'gst_analytics_version': provenance['gst_analytics_version']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(json.dumps(verify(args.root)))


if __name__ == '__main__':
    main()
