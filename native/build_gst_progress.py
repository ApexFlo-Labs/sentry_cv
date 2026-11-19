#!/usr/bin/env python3
"""Build the small GStreamer observer with public stable C APIs; no SDK upgrade."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=root/'lib/libsentinel_gst_progress.so')
    p.add_argument('--provenance',type=Path,default=root/'build-info/gst_progress.json')
    a = p.parse_args()
    source = root/'src/gst_progress.c'
    flags = shlex.split(subprocess.check_output([
        'pkg-config','--cflags','--libs','gstreamer-1.0'],text=True))
    command = ['gcc','-std=c11','-O2','-DNDEBUG','-march=x86-64','-mtune=generic',
               '-fPIC','-shared','-Wall','-Wextra','-Werror','-Wl,-z,defs',
               str(source),*flags,'-o',str(a.output)]
    a.output.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(command,check=True)
    a.provenance.parent.mkdir(parents=True,exist_ok=True)
    a.provenance.write_text(json.dumps({
        'utc':datetime.now(timezone.utc).isoformat(),'command':command,
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'binary_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),
        'compiler':subprocess.check_output(['gcc','--version'],text=True),
        'headers_gstreamer':subprocess.check_output(['pkg-config','--modversion','gstreamer-1.0'],text=True).strip(),
        'scope':'Public GStreamer1.x/GLib2 stable C ABI. Actual target-image native fixture is mandatory.'
    },indent=2)+'\n')
    print(a.output)


if __name__=='__main__':main()
