#!/usr/bin/env python3
"""Bounded diagnostic-only startup repeats after an observed native crash.

Uses the original failed command plus an explicit hashed diagnostic tracer or
configuration hook. No CV image/model/policy modification or qualifying capacity
result. Stop for a native/setup failure or any guard exclusion.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess


def save(path,value):
    tmp=path.with_suffix('.repeat.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path);os.chown(path,1000,1000)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--count',type=int,default=6)
    p.add_argument('--diagnostic-hook',type=Path)
    p.add_argument('--case-prefix',default='diag-native-startup-SA4x8-preload')
    p.add_argument('--run-id',default='remote64-c14')
    p.add_argument('--reference-command',type=Path)
    p.add_argument('--uninstrumented',action='store_true',
                   help='Fresh baked-image startup regression, no hook or tracer; short hold still not capacity qualification')
    a=p.parse_args();r=a.root.resolve();art=r/'artifacts'
    assert 1<=a.count<=10
    original=json.loads((a.reference_command or art/'turbo-off-next-SA-4x8-command.json').read_text())['command']
    tracer=art/'native-crash-tracer-build/native-crash-trace.so'
    assert not (a.uninstrumented and a.diagnostic_hook)
    if not a.uninstrumented and not a.diagnostic_hook:
        assert json.loads((tracer.parent/'report.json').read_text())['passed']
    rows=[]
    interrupted=[False]
    active=[None]
    def stop(*unused):
        interrupted[0]=True
        if active[0] is not None and active[0].poll() is None:
            active[0].terminate()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,stop)
    try:
        for attempt in range(1,a.count+1):
            assert not interrupted[0] and not (r/'STOP').exists(),'User stop'
            assert not subprocess.check_output(['docker','ps','-q','--filter',
                'label=sentinel.validation_run='+a.run_id],text=True).strip(),'Owned CV still active'
            case=f'{a.case_prefix}-r{attempt}'
            command=original[:]
            command[command.index('--case')+1]=case
            command[command.index('--steady-seconds')+1]='30'
            command[command.index('--warmup-seconds')+1]='0'
            if a.uninstrumented:
                pass
            elif a.diagnostic_hook:
                assert a.diagnostic_hook.is_file()
                command+=['--diagnostic-hook',str(a.diagnostic_hook)]
            else:
                command+=['--diagnostic-crash-tracer',str(tracer)]
            save(art/(case+'-command.json'),{'command':command,'scope':__doc__})
            status={'utc':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),
                    'case':case,'status':'RUNNING_DIAGNOSTIC_REPEATS','rows':rows,'scope':__doc__}
            save(art/(a.case_prefix+'-status.json'),status)
            with (art/(case+'-controller.log')).open('w') as log:
                guard=subprocess.Popen(command,cwd=r/'repo',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                active[0]=guard
                bridge=subprocess.Popen(['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/remote_case_bridge.py','--root',str(r),
                    '--run-id',a.run_id,'--case',case,'--guard-pid',str(guard.pid),
                    '--owner','1000','--seconds','172800'],cwd=r/'repo',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                code=guard.wait()
                bridge.wait(timeout=120)
                active[0]=None
            directory=art/'runs'/a.run_id/case
            verdict=json.loads((directory/'status.json').read_text())
            reports=list(directory.glob('raw-*/report.json'))
            logs=list(directory.glob('raw-*/container.log'))
            assert reports, 'Diagnostic did not reach a final report; inspect setup'
            signals=[str(path) for path in logs if any(token in path.read_text(errors='replace') for token in [
                'SENTRY_TEST_NATIVE_BACKTRACE','Fatal Python error: Segmentation fault','stack smashing detected'])]
            fatals={str(path):json.loads(path.read_text()).get('fatal_lines',[])
                    for path in reports}
            if a.diagnostic_hook:
                assert any('SENTRY_TEST_COMPILATION_THREADS' in path.read_text(errors='replace') for path in logs), 'No compilation control reached native graph'
            rows.append({'case':case,'guard_exit':code,'guard_status':verdict.get('status'),
                         'reports':[str(path) for path in reports],'native_fault_logs':signals,
                         'fatal_diagnostics':fatals,
                         'capacity_qualified':False})
            save(art/(a.case_prefix+'-results.json'),rows)
            assert verdict.get('status') in ['PASS','FAIL'],'Guard exclusion requires review'
            if signals or any(fatals.values()):
                save(art/(a.case_prefix+'-status.json'),{**status,'status':'NATIVE_FAULT_CAPTURED_REQUIRES_REVIEW','rows':rows})
                return
        save(art/(a.case_prefix+'-status.json'),{**status,'status':'BOUNDED_REPEATS_NO_FAULT_NOT_A_FIX','rows':rows})
    except BaseException as exc:
        save(art/(a.case_prefix+'-status.json'),{'utc':datetime.now(timezone.utc).isoformat(),
             'status':'STOPPED' if interrupted[0] or (r/'STOP').exists() else 'NEEDS_REVIEW','reason':str(exc),'rows':rows})
        raise


if __name__=='__main__':
    main()
