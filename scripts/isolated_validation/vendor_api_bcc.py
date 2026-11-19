#!/usr/bin/python3
"""Read-only vendor API uprobes at hash-verified ELF addresses (BCC backend)."""
import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from bcc import BPF
from vendor_api_observer import DRIVER_SHA, OFFSETS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    pid = os.getpid() if args.smoke else args.pid
    if not pid or hashlib.sha256(args.library.read_bytes()).hexdigest() != DRIVER_SHA:
        raise ValueError('exact driver hash and target PID required')
    source = '#include <uapi/linux/ptrace.h>\nstruct Key {u32 tid; u32 kind;};\n'
    source += 'BPF_HASH(active, struct Key, u64, 1024); BPF_ARRAY(entered,u64,16); BPF_ARRAY(returned,u64,16);\n'
    for index, _ in enumerate(OFFSETS, 1):
        source += f'''int enter{index}(struct pt_regs*ctx) {{
          u64 id=bpf_get_current_pid_tgid(); if((id>>32)!={pid})return 0;
          struct Key key={{.tid=(u32)id,.kind={index}}};u64 now=bpf_ktime_get_ns();active.update(&key,&now);
          u32 kind={index};u64*v=entered.lookup(&kind);if(v)__sync_fetch_and_add(v,1);return 0;}}
        int leave{index}(struct pt_regs*ctx) {{
          u64 id=bpf_get_current_pid_tgid();if((id>>32)!={pid})return 0;
          struct Key key={{.tid=(u32)id,.kind={index}}};active.delete(&key);
          u32 kind={index};u64*v=returned.lookup(&kind);if(v)__sync_fetch_and_add(v,1);return 0;}}\n'''
    args.output.with_suffix('.bpf.c').write_text(source)
    tracer = BPF(text=source)
    for index, (name, address) in enumerate(OFFSETS.items(), 1):
        tracer.attach_uprobe(name=str(args.library), addr=address, fn_name=f'enter{index}', pid=pid)
        tracer.attach_uretprobe(name=str(args.library), addr=address, fn_name=f'leave{index}', pid=pid)
    if args.smoke:
        loader = ctypes.CDLL('/usr/lib/x86_64-linux-gnu/libOpenCL.so.1')
        platform = ctypes.c_void_p()
        count = ctypes.c_uint()
        assert loader.clGetPlatformIDs(1,ctypes.byref(platform),ctypes.byref(count)) == 0
        dispatch = ctypes.cast(platform,ctypes.POINTER(ctypes.c_void_p))[0]
        # Driver entrypoint from the checked ICD dispatch map, never a GPU command.
        base = next(int(line.split('-')[0],16) for line in Path('/proc/self/maps').read_text().splitlines()
                    if 'libigdrcl.so' in line and int(line.split()[2],16)==0)
        wait = ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_uint,ctypes.c_void_p)(base+OFFSETS['clWaitForEvents'])
        # The ICD loader and vendor entrypoint can differ on invalid arguments;
        # record the vendor result rather than asserting loader semantics.
        smoke_status = wait(0,None)
    deadline = time.monotonic() + (0 if args.smoke else args.seconds)
    try:
        with args.output.open('x') as output:
            while True:
                now = time.monotonic_ns()
                record = dict(utc=datetime.now(timezone.utc).isoformat(),monotonic_ns=now,pid=pid,
                    active=[dict(tid=k.tid,api=list(OFFSETS)[k.kind-1],age_seconds=(now-v.value)/1e9)
                            for k,v in tracer['active'].items()],
                    entered={name:tracer['entered'][ctypes.c_int(i)].value for i,name in enumerate(OFFSETS,1)},
                    returned={name:tracer['returned'][ctypes.c_int(i)].value for i,name in enumerate(OFFSETS,1)})
                output.write(json.dumps(record)+'\n');output.flush()
                if args.smoke:
                    record['invalid_argument_vendor_status'] = smoke_status
                    assert record['entered']['clWaitForEvents']==1 and record['returned']['clWaitForEvents']==1
                    print('BCC vendor wait entry/return smoke PASS; status='+str(smoke_status),flush=True)
                if time.monotonic() >= deadline or not Path(f'/proc/{pid}').exists():
                    break
                time.sleep(1)
    finally:
        tracer.cleanup()


if __name__ == '__main__':
    main()
