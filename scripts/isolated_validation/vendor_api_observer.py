#!/usr/bin/env python3
"""Bounded read-only probes on one owned CV worker's vendor API entrypoints.

Offsets apply ONLY to the SHA-checked installed driver. No driver calls, GPU
commands, host settings or inferior memory writes are performed by this tool.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

DRIVER_SHA = '7ebd1da3c9acefa35765539359636ec66471d44ed67eead88bdffd0e5dc86089'
OFFSETS = {
    'clWaitForEvents': 0xe6be0, 'clFinish': 0xe1790,
    'clEnqueueReadBuffer': 0xe8660, 'clEnqueueWriteBuffer': 0xf0c40,
    'clEnqueueMapBuffer': 0xf3c80, 'clEnqueueMapImage': 0xf4290,
    'clEnqueueAcquireVA_APIMediaSurfacesINTEL': 0x181d40,
    'clEnqueueReleaseVA_APIMediaSurfacesINTEL': 0x181f00,
    'clEnqueueMemcpyINTEL': 0x103450,
    'clEnqueueNDRangeKernel': 0xf5570,
    'clFlush': 0xe14d0,
}


def program(library, pid):
    parts = ['BEGIN { printf("vendor API probe started\\n"); }']
    for index, (name, offset) in enumerate(OFFSETS.items(), 1):
        target = f'{library}:0x{offset:x}'
        parts.append(f'uprobe:{target} /pid == {pid}/ {{ '
                     f'@started[tid] = nsecs; @kind[tid] = {index}; @entered[{index}] = count(); }}')
        parts.append(f'uretprobe:{target} /pid == {pid}/ {{ '
                     f'@returned[{index}] = count(); '
                     'if (@started[tid] && nsecs-@started[tid] > 1000000000) { '
                     f'printf("long_return api={name} tid=%d ns=%llu status=%d\\n", '
                     'tid, nsecs-@started[tid], retval); } '
                     'delete(@started[tid]); delete(@kind[tid]); }')
    parts.append('interval:s:1 { printf("SNAPSHOT monotonic_ns=%llu\\n", nsecs); '
                 'print(@started); print(@kind); print(@entered); print(@returned); }')
    return '\n'.join(parts) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--case', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=900)
    p.add_argument('--port', type=int, default=18380)
    a = p.parse_args()
    a.output.mkdir(exist_ok=False, parents=True)
    deadline = time.monotonic() + a.seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f'http://127.0.0.1:{a.port}/healthz', timeout=.7) as response:
                health = json.load(response)
            cameras = health.get('telemetry', {}).get('cameras', [])
            cameras = [c for c in cameras if c.get('processing', {}).get('totals', {}).get('inference_frames', 0) > 50]
            ids = subprocess.check_output(['podman','ps','-q','--filter',f'label=sentinel.validation_run={a.run_id}',
                '--filter',f'label=sentinel.validation_case={a.case}'], text=True).split()
            if not cameras or len(ids) != 1:
                time.sleep(.5)
                continue
            pid = cameras[0]['pid']
            top = subprocess.check_output(['podman','top',ids[0],'hpid','pid','comm'], text=True)
            matches = [row.split() for row in top.splitlines()[1:]
                       if len(row.split()) >= 3 and row.split()[1] == str(pid)]
            if len(matches) != 1:
                raise ValueError('worker PID not uniquely identified')
            hpid = int(matches[0][0])
            library = Path(f'/proc/{hpid}/root/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so')
            digest = hashlib.sha256(library.read_bytes()).hexdigest()
            if digest != DRIVER_SHA:
                raise RuntimeError('vendor API offsets require the exact verified driver')
            source = a.output / 'vendor-api.bt'
            source.write_text(program(library, hpid))
            command = ['sudo','-n','timeout','--signal=INT',str(max(1,int(deadline-time.monotonic()))),
                       '/usr/bin/python3',str(Path(__file__).with_name('vendor_api_bcc.py').resolve()),
                       '--pid',str(hpid),'--library',str(library),'--seconds',str(max(1,int(deadline-time.monotonic()))),
                       '--output',str((a.output/'vendor-api.jsonl').resolve())]
            record = dict(utc=datetime.now(timezone.utc).isoformat(), container_id=ids[0],
                host_pid=hpid, namespace_pid=pid, driver_sha256=digest,
                probe_kinds={i:n for i,n in enumerate(OFFSETS,1)}, command=command,
                scope='Read-only API-entry diagnostic, not capacity. Nested vendor calls may replace the current per-thread kind.')
            (a.output/'launch.json').write_text(json.dumps(record,indent=2))
            with (a.output/'vendor-api.log').open('x') as log:
                result = subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
            record.update(returncode=result.returncode,finished_utc=datetime.now(timezone.utc).isoformat())
            (a.output/'result.json').write_text(json.dumps(record,indent=2))
            return result.returncode
        except (OSError, ValueError, TimeoutError) as error:
            (a.output/'last-poll-error.txt').write_text(type(error).__name__+': '+str(error))
            time.sleep(.5)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
