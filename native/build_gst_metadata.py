#!/usr/bin/env python3
"""Build against the exact image's public GstAnalytics headers and libraries."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=root/'lib/libsentinel_gst_metadata.so')
    parser.add_argument('--provenance', type=Path, default=root/'build-info/gst_metadata.json')
    args = parser.parse_args()
    source = root/'src/gst_detection_metadata.c'
    flags = shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs',
                                               'gstreamer-analytics-1.0'],text=True))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    command = ['gcc','-std=c11','-O2','-DNDEBUG','-march=x86-64','-mtune=generic',
        '-fPIC','-shared','-Wall','-Wextra','-Werror','-Wl,-z,defs',str(source),*flags,'-o',str(args.output)]
    subprocess.run(command,check=True)
    args.provenance.parent.mkdir(parents=True,exist_ok=True)
    args.provenance.write_text(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
        command=command,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest(),
        gst_analytics_version=subprocess.check_output(['pkg-config','--modversion',
            'gstreamer-analytics-1.0'],text=True).strip(),
        scope='Public read-only GstAnalytics ABI; same-native-sample parity required before activation'),indent=2))


if __name__ == '__main__':
    main()
