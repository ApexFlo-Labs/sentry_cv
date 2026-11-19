#!/usr/bin/env python3
"""Read-only diagnostic on one owned validation container, not a capacity test.

Wait for stale completed output with pending native admissions. Snapshot Python
and 32 native frames before the production recovery deadline. GDB suspends the
owned worker briefly, so this attempt must remain explicitly diagnostic.
"""
import argparse,json,os,re,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen


def clean(text):
 return re.sub(r'\b(?:rtsp|rtsps|https?|tcp)://[^\s\"\'<>]+','<redacted-url>',text,flags=re.I)


def deliberately_paused(record, now=None):
 if record.get('status')=='PAUSED_STARTUP_COOLING':return True
 # Cached health and in-flight frames need time to refresh after SIGCONT.
 now=time.monotonic() if now is None else now
 return any(0 <= now-pause['end_monotonic'] < 5
            for pause in record.get('startup_pauses',[])if pause.get('end_monotonic')is not None)


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',choices=['podman','docker'],default='podman');p.add_argument('--run-id',required=True);p.add_argument('--case',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--seconds',type=int,default=540);p.add_argument('--port',type=int,default=18380);p.add_argument('--gdb-script',type=Path);p.add_argument('--no-python-signal',action='store_true');p.add_argument('--guard-status',type=Path);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 deadline=time.monotonic()+a.seconds
 guard_status=a.guard_status
 while time.monotonic()<deadline:
  try:
   guard_record=json.loads(guard_status.read_text()) if guard_status and guard_status.exists() else {}
   if guard_record.get('status') in {'PASS','FAIL','BLOCKED_THERMAL','BLOCKED_RESOURCES','CANCELLED'}:
    (a.output/'capture.json').write_text(json.dumps({'captured':False,'reason':'owned guard finished before qualifying native stall','guard_status':guard_record['status']}));return 2
   # A thermal startup SIGSTOP is deliberate, not a native inference stall.
   if deliberately_paused(guard_record):
    time.sleep(.25);continue
   with urlopen(f'http://127.0.0.1:{a.port}/healthz',timeout=.7)as f:health=json.load(f)
   cameras=health.get('telemetry',{}).get('cameras',[])
   stale=[c for c in cameras if c.get('processing',{}).get('totals',{}).get('inference_frames',0)>100 and c.get('processing',{}).get('gauges',{}).get('dls_pending_admissions',0)>0 and (c.get('decoder',{}).get('last_frame_age_seconds')or 0)>3]
   if len(stale)<2:time.sleep(.25);continue
   ids=subprocess.check_output([a.engine,'ps','-q','--filter',f'label=sentinel.validation_run={a.run_id}','--filter',f'label=sentinel.validation_case={a.case}'],text=True).split()
   if len(ids)!=1:raise RuntimeError('exact owned container required')
   cid=ids[0];worker=stale[0]['pid']
   if a.engine=='docker':
    top=subprocess.check_output([a.engine,'top',cid,'-eo','pid,comm'],text=True);matches=[]
    for row in top.splitlines()[1:]:
     fields=row.split()
     if not fields or not fields[0].isdigit():continue
     try:ns=next(l.split()[1:]for l in Path(f'/proc/{fields[0]}/status').read_text().splitlines()if l.startswith('NSpid:'))
     except (FileNotFoundError,StopIteration):continue
     if ns and ns[-1]==str(worker):matches.append([fields[0],str(worker)])
   else:
    top=subprocess.check_output([a.engine,'top',cid,'hpid','pid','comm'],text=True)
    matches=[row.split()for row in top.splitlines()[1:]if len(row.split())>=3 and row.split()[1]==str(worker)]
   if len(matches)!=1:raise RuntimeError('exact worker PID mapping required')
   hpid=int(matches[0][0]);(a.output/'thread-namespace-map.json').write_text(json.dumps({task.name:[line for line in (task/'status').read_text().splitlines()if line.startswith(('Name:', 'NSpid:'))]for task in Path(f'/proc/{hpid}/task').iterdir()},indent=2));(a.output/'stalled-health.json').write_text(json.dumps(health,indent=2));(a.output/'processes.txt').write_text(top)
   command=['sudo','-n','gdb','-q','-batch','-ex','set pagination off','-ex','set auto-load safe-path /nonexistent','-ex','set debuginfod enabled off','-ex',f'set sysroot /proc/{hpid}/root','-p',str(hpid),'-ex','info proc mappings','-ex','thread apply all bt 32']
   if a.gdb_script:command+=['-ex','source '+str(a.gdb_script.resolve())]
   command+=['-ex','detach']
   record={'utc':datetime.now(timezone.utc).isoformat(),'container_id':cid,'namespace_worker_pid':worker,'host_worker_pid':hpid,'scope':'Diagnostic attachment suspends only owned worker; not capacity qualification','command':command}
   # faulthandler is enabled by an explicit read-only hook in this test.
   if not a.no_python_signal:
    subprocess.run(['sudo','-n','kill','-USR1',str(hpid)],check=True,timeout=3)
   began=time.monotonic()
   result=subprocess.run(command,capture_output=True,text=True,timeout=25)
   (a.output/'native-thread-stacks.txt').write_text(clean(result.stdout+result.stderr));record.update(returncode=result.returncode,duration_seconds=time.monotonic()-began)
   (a.output/'capture.json').write_text(json.dumps(record,indent=2));print(json.dumps(record),flush=True);return 0
  except (OSError,ValueError,TimeoutError)as e:
   (a.output/'last-poll-error.txt').write_text(clean(str(e)));time.sleep(.5)
 (a.output/'capture.json').write_text(json.dumps({'captured':False,'reason':'no qualifying stale output before bounded deadline'}));return 2


if __name__=='__main__':raise SystemExit(main())
