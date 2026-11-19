#!/usr/bin/env python3
"""Instrumented pixel parity for one frozen candidate, prefetch disabled/enabled.

Retains existing case evidence when resuming. This does not qualify throughput.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time


def recorded_command(record):
    """Accept the archived command envelope or legacy argv without guessing."""
    command=record.get('command') if isinstance(record,dict) else record
    if (not isinstance(command,list) or not command
            or not all(isinstance(value,str) for value in command)
            or not all(flag in command for flag in ['--case','--image','--steady-seconds'])):
        raise ValueError('Expected a recorded guarded image-test command')
    return command[:]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--base-command', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    a = p.parse_args()
    r = a.root.resolve()
    base = recorded_command(json.loads(a.base_command.read_text()))
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    env['PATH'] = '/home/admin1/sentry_cv/.venv/bin:'+env['PATH']
    reports = []
    for mode in [0, 1]:
        case = 'pixel-parity-prefetch-'+str(mode)
        directory = r/'artifacts/runs'/a.run_id/case
        if not directory.exists():
            cmd = base[:]
            cmd[cmd.index('--case')+1] = case
            cmd[cmd.index('--steady-seconds')+1] = '20'
            cmd += ['--diagnostic-hook',str(r/'repo/scripts/isolated_validation/pixel_hash_diagnostic_hook.py'),
                    '--container-env','SENTINEL_DLS_BGR_PREFETCH='+str(mode)]
            (r/'artifacts'/(case+'-command.json')).write_text(json.dumps(cmd,indent=2))
            with (r/'artifacts'/(case+'-guard.log')).open('w') as log:
                guard = subprocess.Popen(cmd,cwd=r/'repo',env=env,stdout=log,
                                         stderr=subprocess.STDOUT,start_new_session=True)
                subprocess.Popen(['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/remote_case_bridge.py','--root',str(r),
                    '--run-id',a.run_id,'--case',case,'--guard-pid',str(guard.pid),
                    '--owner','1000','--seconds','10800'],cwd=r/'repo',env=env,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        while True:
            if (r/'STOP').exists():
                raise InterruptedError('User stop; case bridge handles owned cleanup')
            try:
                status = json.loads((directory/'status.json').read_text())
            except (FileNotFoundError,ValueError):
                time.sleep(1)
                continue
            if 'supervisor_exit_code' in status:
                break
            time.sleep(1)
        if status['status'].startswith('BLOCKED'):
            raise RuntimeError('Parity case excluded: '+status['status'])
        raw = None
        for f in sorted(directory.glob('attempt-*.stdout.log'),key=lambda p:p.stat().st_mtime):
            for line in f.read_text().splitlines():
                try:
                    raw = json.loads(line).get('artifact_directory',raw)
                except (ValueError,AttributeError):
                    pass
        raw = Path(raw)
        report = json.loads((raw/'report.json').read_text())
        if report['fatal_lines']:
            raise RuntimeError('Native fault invalidates pixel comparison')
        data = []
        for line in (raw/'container.log').read_text(errors='replace').splitlines():
            try:
                value = json.loads(line)
                if value.get('event') == 'validation_source_pixels':
                    data.append(value)
            except ValueError:
                pass
        health = json.loads((raw/'healthz-final.json').read_text())
        totals = health['telemetry']['cameras'][0]['processing']['totals']
        reports.append({'mode':mode,'case':case,'raw':str(raw),'hashes':data,
                        'prefetched_frames':totals.get('primary_bgr_prefetched_frames',0),
                        'image_id':report['image_config_id']})
    assert reports[0]['image_id'] == reports[1]['image_id'], 'Different candidate images'
    maps = [{v['pts_ns']:(v['shape'],v['bgr_sha256']) for v in x['hashes']} for x in reports]
    common = set(maps[0]) & set(maps[1])
    differences = [pts for pts in common if maps[0][pts] != maps[1][pts]]
    result = {'utc':datetime.now(timezone.utc).isoformat(),'scope':__doc__,
              'passed':len(common)>=30 and not differences and reports[1]['prefetched_frames']>0,
              'common_frames':len(common),'different_pixels':len(differences),
              'difference_pts_ns':differences,'runs':reports}
    (r/'artifacts/pixel-parity-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='runs'}),flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
