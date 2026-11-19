#!/usr/bin/env python3
"""Read-only off-host campaign completion/audit watcher.

Persists disconnects and terminal failures, mirrors exact-image evidence, runs
offline audits and writes a review report. Does not restart a workload, mutate
power/policy, publish, or grant whole-catalog production qualification.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time


def save(path, value):
    temp=path.with_suffix('.completion.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n')
    temp.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--remote-root',required=True)
    parser.add_argument('--host',default='admin1@192.168.1.64')
    parser.add_argument('--image',required=True)
    parser.add_argument('--run-id',default='remote64-c14')
    parser.add_argument('--seconds',type=int,default=43200)
    args=parser.parse_args()
    root=args.root.resolve()
    repo=Path(__file__).resolve().parents[2]
    lock=(root/'COMPLETION_WATCH.lock').open('a')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    deadline=time.monotonic()+args.seconds
    last=None
    destination=root/'remote-artifacts'
    def checkpoint(status,**extra):
        save(root/'completion-watch.json',{'utc':datetime.now(timezone.utc).isoformat(),
             'pid':os.getpid(),'status':status,'image_config_id':args.image,
             'capacity_qualified':False,**extra})
    def handoff(message):
        entry='\n\n### Automatic '+args.run_id+' evidence-review checkpoint — '+datetime.now(timezone.utc).isoformat()+'\n\n'+message+'\n'
        for p in [repo/'OPTIMIZATION_HANDOFF.md',Path('/home/admin1/Downloads/sentry-optimization-handoff.md')]:
            with p.open('a') as stream:stream.write(entry)
    checkpoint('WAITING_POSTCHECKS')
    while time.monotonic()<deadline:
        try:
            response=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',args.host,
                'cat',args.remote_root+'/CAMPAIGN_CURRENT.json'],capture_output=True,text=True,timeout=20)
            response.check_returncode()
            state=json.loads(response.stdout)
            assert state['image']==args.image,'Remote candidate changed'
            status=state.get('status')
            if state!=last:
                checkpoint('MONITORING',remote_state=state)
                last=state
            if status in ['NEEDS_REVIEW','STOPPED']:
                checkpoint('REMOTE_REQUIRES_REVIEW',remote_state=state)
                handoff('Native/postcheck campaign stopped for review; no completion claim. '+json.dumps(state))
                return 2
            if status=='POSTCHECKS_FINISHED_REQUIRES_REVIEW':
                break
        except AssertionError:
            raise
        except (OSError,subprocess.SubprocessError,ValueError) as exc:
            checkpoint('CONNECTION_OR_READ_RETRY',reason=str(exc)[-1000:])
        time.sleep(30)
    else:
        checkpoint('WATCH_DEADLINE_REQUIRES_REVIEW')
        return 2
    checkpoint('FINAL_MIRROR_AND_OFFLINE_AUDIT')
    subprocess.run(['rsync','-a','--copy-dirlinks','--exclude=*.bridge.tmp',
        args.host+':'+args.remote_root+'/artifacts/',str(destination)+'/'],check=True,timeout=900)
    commands=[['audit_captured_packages.py',str(destination),'--output',str(root/'package-integrity-audit-final.json')],
              ['analyze_observed_engines.py',str(root),'--output',str(root/'hardware-steady-analysis.json')],
              ['analyze_stage_work.py',str(root),'--output',str(root/'stage-work-final.json')],
              ['summarize_remote_campaign.py',str(root),'--run-id',args.run_id]]
    audit_runs=[]
    for command in commands:
        with (root/('final-'+command[0]+'.log')).open('w') as log:
            result=subprocess.run(['python3',str(repo/'scripts/isolated_validation'/command[0]),*command[1:]],
                cwd=repo,stdout=log,stderr=subprocess.STDOUT,timeout=900)
            audit_runs.append({'command':command,'exit_code':result.returncode})
    save(root/'offline-review-results.json',audit_runs)
    hour_reports=list((destination/'runs'/args.run_id).glob('*one-hour/raw-*/report.json'))
    hour_results=[]
    for index,report in enumerate(hour_reports):
        out=root/('sustained-run-'+str(index)+'.json')
        subprocess.run(['python3',str(repo/'scripts/isolated_validation/analyze_sustained_run.py'),
            str(report),'--output',str(out)],check=True,timeout=120)
        hour_results.append(json.loads(out.read_text()))
    hour_passed=any(d['probe_passed'] and d['full_hour_observed'] for d in hour_results)
    rows=json.loads((destination/'continuation-ledger.json').read_text())
    frontier=json.loads((destination/'continuation-frontier.json').read_text())
    mixed_path=destination/'mixed-role-results.json'
    mixed=json.loads(mixed_path.read_text()) if mixed_path.exists() else []
    audit_path=root/'package-integrity-audit-final.json'
    audit=json.loads(audit_path.read_text()) if audit_path.exists() else {'passed':False,'cases':[]}
    lines=['# '+args.run_id+' executed-batch review', '',
        'Exact Docker config ID: `'+args.image+'`. Local candidate; not a registry digest or publication.', '',
        'Tests used CPU turbo disabled and full16 logical CPUs eligible. Scene was enabled on every camera. '
        'Repeated original footage across independent connections is not a diverse physical-camera dataset. '
        'This report does not qualify every catalog case or stock-power production capacity.', '',
        '| Workload | Target FPS/camera | Last short passing cameras | First short failing cameras |',
        '|---|---:|---:|---:|']
    for key,value in frontier.items():
        profile,fps=key.rsplit('-',1)
        lines.append(f"| {'Scene only' if profile=='S' else 'All apps enabled, people corpus'} | {fps} | {value['last_short_passing_count']} | {value['first_short_failing_count']} |")
    lines+=['', 'Short acceptance: each camera mean at least95% of target and every sampled window at least90%, '
            'plus scene-package coverage, health and timestamp checks. Short boundaries are not continuous guarantees.', '',
            'The sustained-run JSON files preserve the actual hour verdicts, camera completion windows, '
            'memory slopes, first/last ten-minute medians, frame age and worker/session changes. '
            'A memory slope alone does not prove a leak or prove leak freedom.', '',
            '| Mixed test | Cameras | FPS/camera | Identity cameras, if applicable | Verdict |',
            '|---|---:|---:|---:|---|']
    for row in mixed:
        lines.append(f"| {Path(row['report_path']).parents[1].name} | {row['camera_count']} | {row['fps']} | {row.get('identity_cameras','—')} | {'PASS' if row['passed'] else 'FAIL'} |")
    lines+=['',f"Observed passing hour: {hour_passed}. Package audit passed: {audit['passed']}. "
            f"Offline commands with nonzero exit: {sum(v['exit_code'] != 0 for v in audit_runs)}.",
            '',f"Captured-package audit: {sum(v['counts'].get('packages',0) for v in audit['cases'])} records; "
            f"{sum(v['counts'].get('JPEGs_independently_decoded',0) for v in audit['cases'])} saved JPEGs independently decoded.", '',
            'Read RESULTS_CURRENT.json/.md for per-camera completion, sampled cgroup memory/CPU, stage timings, '
            'source-clock latency and exact per-case artifacts. Hardware-steady-analysis.json contains '
            'separate raw decode FPS and node GPU-engine/NPU/power/temperature measurements. '
            'Independent marked-pixel latency is a separate oracle.', '',
            'Delivery scope ends at complete-package acknowledgement by the capture agent API. '
            'Recognition accuracy, real-agent storage/Management delivery and all unexecuted catalog variants '
            'remain outside this executed-batch report. Review excluded thermal attempts and all FAILs. '
            'Original host power/services still require explicit restoration; this watcher is read-only.', '',
            f"Ledger rows: {len(rows)}. Failed rows retained: {sum(v['status']!='PASS' for v in rows)}."]
    (root/'EXECUTED_BATCH_REVIEW.md').write_text('\n'.join(lines)+'\n')
    checkpoint('EXECUTED_BATCH_EVIDENCE_READY_FOR_REVIEW',report=str(root/'EXECUTED_BATCH_REVIEW.md'))
    handoff('Queued batch finished and exact-image evidence mirrored. Offline package/JPEG audit and sustained trends saved in '+str(root)+
            '. Review EXECUTED_BATCH_REVIEW.md, all FAILs, exclusions and remaining catalog scopes. No blanket capacity or production '
            'qualification, no publication. Power/service restoration remains explicit work; this watcher did not mutate the host.')
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Retain a crash signal even when a final audit cannot complete.
        print(type(exc).__name__+': '+str(exc),flush=True)
        raise
