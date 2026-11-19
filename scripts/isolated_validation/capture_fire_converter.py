#!/usr/bin/env python3
"""Read a real YOLOv8 converter input without replacing its postprocessor.

Explicit root-only debugger diagnostic on an owned validation container.
Attachment interrupts execution briefly; never use its run as capacity evidence.
No model, input, output, gate or instruction is modified. Store the library
fingerprint, symbol offset, exact tensor bytes and debugger log for review.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time
from urllib.request import urlopen


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--run-id',default='remote64-c15')
    p.add_argument('--health-port',type=int,default=18380)
    p.add_argument('--require-black-roi',action='store_true')
    p.add_argument('--case-root',type=Path,default=Path('/home/admin1/Documents/sentry-validation-20261111-c15/artifacts/runs'))
    p.add_argument('--timeout',type=int,default=600)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    deadline=time.monotonic()+a.timeout;target=None
    while time.monotonic()<deadline:
        if a.require_black_roi:
            raw=None
            for logfile in (a.case_root/a.run_id/a.case).glob('attempt-*.stdout.log'):
                for line in logfile.read_text().splitlines():
                    try:raw=json.loads(line).get('artifact_directory',raw)
                    except (ValueError,AttributeError):pass
            trace=Path(raw)/'delivery/fire-path-diagnostic/trace.jsonl'if raw else None
            positive=[]
            if trace and trace.exists():
                for line in trace.read_text().splitlines():
                    try:value=json.loads(line)
                    except ValueError:continue
                    if (value.get('stage')=='firedet.src'and value.get('pixels',{}).get('max')==0
                            and value.get('rois')):positive.append(value)
            if not positive:time.sleep(.2);continue
            (a.output/'trigger.json').write_text(json.dumps(positive[-1],indent=2)+'\n')
        try:
            with urlopen('http://127.0.0.1:'+str(a.health_port)+'/healthz',timeout=2)as reply:
                health=json.load(reply)
            reported_pids={int(c['pid'])for c in health.get('telemetry',{}).get('cameras',[])
                if c.get('pid')}
        except (OSError,ValueError,KeyError):
            time.sleep(.2);continue
        if not reported_pids:time.sleep(.2);continue
        ids=subprocess.check_output(['docker','ps','-q','--filter',
            'label=sentinel.validation_run='+a.run_id,'--filter',
            'label=sentinel.validation_case='+a.case],text=True).splitlines()
        for container in ids:
            top=subprocess.check_output(['docker','top',container,'-eo','pid'],text=True)
            for line in top.splitlines()[1:]:
                if not line.strip().isdigit():continue
                pid=int(line.strip());root=Path('/proc')/str(pid)
                try:
                    status=(root/'status').read_text()
                    ns=[x.split()[1:]for x in status.splitlines()if x.startswith('NSpid:')]
                    if not ns or int(ns[0][-1])not in reported_pids:continue
                    maps=(root/'maps').read_text()
                except OSError:continue
                found=[x.split()for x in maps.splitlines()if 'libgstvideoanalytics.so'in x]
                if not found:continue
                module=found[0][-1];library=root/'root'/module.lstrip('/')
                symbols=subprocess.check_output(['nm','-C',str(library)],text=True)
                matches=[x for x in symbols.splitlines()if
                    'YOLOv8Converter::parseOutputBlob('in x and '[clone'not in x]
                if len(matches)!=1:raise RuntimeError('Expected one real converter symbol')
                fields=found[0];base=int(fields[0].split('-')[0],16)-int(fields[2],16)
                offset=int(matches[0].split()[0],16)
                target={'pid':pid,'container':container,'module':module,'base':base,
                    'symbol_offset':offset,'symbol':matches[0],
                    'library_sha256':hashlib.sha256(library.read_bytes()).hexdigest()}
                break
            if target:break
        if target:break
        time.sleep(.2)
    if not target:raise RuntimeError('Owned native converter process not found')
    (a.output/'target.json').write_text(json.dumps(target,indent=2)+'\n')
    # x86-64 SysV C++ member call: rdi=this, rsi=data, rdx=dimension vector.
    # Read and validate dimensions before reading tensor data.
    code="""set pagination off
set confirm off
set print thread-events off
break *ADDRESS
commands
silent
python
import gdb,struct,json,time,hashlib
from pathlib import Path
root=Path(OUTPUT)
inferior=gdb.selected_inferior()
dimension_vector=int(gdb.parse_and_eval('$rdx'))
begin,end=struct.unpack('<QQ',inferior.read_memory(dimension_vector,16))
assert 0 < end-begin <= 64 and (end-begin)%8==0
dims=list(struct.unpack('<'+'Q'*((end-begin)//8),inferior.read_memory(begin,end-begin)))
assert dims==[1,6,2100], 'Unexpected converter dimensions '+str(dims)
address=int(gdb.parse_and_eval('$rsi'))
data=bytes(inferior.read_memory(address,12600*4))
(root/'converter-input.f32le').write_bytes(data)
values=struct.unpack('<12600f',data)
record={'dims':dims,'bytes':len(data),'max_fire':max(values[8400:10500]),'max_smoke':max(values[10500:12600]),'sha256':hashlib.sha256(data).hexdigest(),'captured_unix':time.time()}
(root/'tensor.json').write_text(json.dumps(record,indent=2)+'\\n')
print(json.dumps(record))
end
disable 1
detach
quit
end
continue
""".replace('ADDRESS',hex(target['base']+target['symbol_offset'])).replace('OUTPUT',repr(str(a.output)))
    cmd=a.output/'capture.gdb';cmd.write_text(code)
    argv=['gdb','-nx','-nh','-q','--batch','-iex','set debuginfod enabled off',
        '-iex','set auto-solib-add off','-p',str(target['pid']),'-x',str(cmd)]
    (a.output/'command.json').write_text(json.dumps(argv,indent=2)+'\n')
    with (a.output/'gdb.log').open('w')as log:
        result=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,timeout=60)
    if result.returncode or not(a.output/'tensor.json').exists():
        raise RuntimeError('Converter capture failed; retain debugger evidence')
    print((a.output/'tensor.json').read_text())


if __name__=='__main__':main()
