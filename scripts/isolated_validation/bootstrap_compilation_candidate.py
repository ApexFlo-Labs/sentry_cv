#!/usr/bin/env python3
"""Fresh-image qualification after the recorded GPU-compiler mitigation.

Require baked checks, three uninstrumented starts and a qualifying one-camera
foundation before launching capacity/hour and exclusive postchecks. No passing
result is inherited from another image. No publication or gate adjustments.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess


def save(path,value):
    temp=path.with_suffix('.bootstrap.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)
    os.chown(path,1000,1000)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--image',required=True)
    p.add_argument('--run-id',required=True)
    a=p.parse_args();r=a.root.resolve();art=r/'artifacts'
    active=[None];stopped=[False]
    env=dict(os.environ,PYTHONUNBUFFERED='1')
    env['PATH']='/home/admin1/sentry_cv/.venv/bin:'+env['PATH']
    def stop(*unused):
        stopped[0]=True
        if active[0] is not None and active[0].poll() is None:active[0].terminate()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,stop)
    def state(status,**extra):
        save(r/'CAMPAIGN_CURRENT.json',{'utc':datetime.now(timezone.utc).isoformat(),
             'controller_pid':os.getpid(),'status':status,'image':a.image,
             'capacity_qualified':False,'power_scope':'CPU turbo disabled',**extra})
    def run(name,cmd):
        assert not stopped[0] and not (r/'STOP').exists(),'User stop'
        save(art/(name+'-command.json'),{'command':cmd,'image':a.image})
        with (art/(name+'-controller.log')).open('w') as log:
            active[0]=subprocess.Popen(cmd,cwd=r/'repo',env=env,stdout=log,
                                       stderr=subprocess.STDOUT,start_new_session=True)
            code=active[0].wait()
        active[0]=None
        assert code==0,name+' failed; inspect evidence'
    try:
        proof=json.loads((art/'baked-checks/report.json').read_text())
        assert proof['result']=='passed' and proof['image']['config_id']==a.image
        assert not (art/'continuation-ledger.json').exists(),'Fresh image requires fresh ledger'
        command=json.loads((art/'startup-reference-command.json').read_text())['command']
        assert command[command.index('--image')+1]==a.image
        prefix='startup-baked-SA4x8'
        state('RUNNING_UNINSTRUMENTED_STARTUP_REGRESSIONS',case=prefix)
        run('baked-startups',['taskset','-c','14,15','python3',
            'scripts/isolated_validation/run_native_startup_repeats.py','--root',str(r),
            '--count','3','--run-id',a.run_id,'--case-prefix',prefix,
            '--reference-command',str(art/'startup-reference-command.json'),'--uninstrumented'])
        checks=json.loads((art/(prefix+'-status.json')).read_text())
        assert checks['status']=='BOUNDED_REPEATS_NO_FAULT_NOT_A_FIX' and len(checks['rows'])==3
        for row in checks['rows']:
            assert not row['native_fault_logs'] and len(row['reports'])==1
            report=json.loads(Path(row['reports'][0]).read_text())
            assert report['image_config_id']==a.image and not report['fatal_lines']
            assert not report['diagnostic_hook_injected'] and not report['diagnostic_opencl_intercept_injected']
            assert not report.get('diagnostic_crash_tracer_injected')
            assert report['final_health_available'] and report['end_health']['cameras_with_recent_frames']==4
            assert report['source_utc_sanity']['passed'] and report['steady_output_coverage']['passed']
        case='baseline-all-people1x8'
        cmd=command[:]
        for key,value in [('--case',case),('--cameras','1'),('--steady-seconds','120'),('--warmup-seconds','20')]:
            cmd[cmd.index(key)+1]=value
        cmd[cmd.index('--source-urls-json')+1]=json.dumps(json.loads((r/'people50-source-urls.json').read_text())[:1])
        save(art/'baseline-command.json',{'command':cmd})
        state('RUNNING_NATIVE_FOUNDATION',case=case)
        with (art/'baseline-controller.log').open('w') as log:
            active[0]=subprocess.Popen(cmd,cwd=r/'repo',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            bridge=subprocess.Popen(['taskset','-c','14,15','python3',
                'scripts/isolated_validation/remote_case_bridge.py','--root',str(r),
                '--run-id',a.run_id,'--case',case,'--guard-pid',str(active[0].pid),
                '--owner','1000','--seconds','172800'],cwd=r/'repo',env=env,stdout=log,
                stderr=subprocess.STDOUT,start_new_session=True)
            code=active[0].wait();bridge.wait(timeout=120)
        active[0]=None
        verdict=json.loads((art/'runs'/a.run_id/case/'status.json').read_text())
        assert code==0 and verdict['status']=='PASS','Fresh native foundation failed'
        state('LAUNCHING_NATIVE_BATCH_AFTER_FOUNDATION')
        commands={'main':['taskset','-c','14,15','python3',
            'scripts/isolated_validation/continue_remote_campaign.py','--root',str(r),
            '--image',a.image,'--sources-json',str(r/'people50-source-urls.json'),
            '--run-id',a.run_id,'--baseline-case',case,
            '--count-plan-json',str(r/'count-plan.json'),
            '--recovery-upstream','rtsps://192.168.1.92:18554/recovery',
            '--recovery-ca',str(r/'fixture-source-ca.crt'),'--recovery-profile','SA',
            '--recovery-fps','8','--prefetch-fault-regression'],
            'postchecks':['taskset','-c','14,15','python3',
            'scripts/isolated_validation/finish_remote_campaign.py','--root',str(r),
            '--image',a.image,'--run-id',a.run_id,'--skip-verified-baked-checks']}
        record={'utc':datetime.now(timezone.utc).isoformat(),'image':a.image,'children':{}}
        for key,cmd in commands.items():
            with (art/(key+'-launch.log')).open('w') as log:
                child=subprocess.Popen(cmd,cwd=r/'repo',env=env,stdout=log,
                    stderr=subprocess.STDOUT,start_new_session=True)
            record['children'][key]={'pid':child.pid,'command':cmd}
        save(art/'native-batch-launch.json',record)
    except BaseException as exc:
        state('STOPPED' if stopped[0] or (r/'STOP').exists() else 'NEEDS_REVIEW',
              reason=type(exc).__name__+': '+str(exc))
        raise


if __name__=='__main__':
    main()
