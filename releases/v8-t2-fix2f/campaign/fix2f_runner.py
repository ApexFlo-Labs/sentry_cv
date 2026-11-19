#!/usr/bin/env python3
"""T2-FIX2f (scene grid + pixel due on grid) test on .64 (root). Same guarded harness/criteria/isolation as T2 campaign; own ledger; each case once.
Queued in HISTORY_CHECK_2026-11-17.md (Waiting 1-3 + plain face/gait capacity)."""
import json, sys, subprocess, signal, os
sys.argv = sys.argv[:1]
import runner as R
R.LEDGER = R.T / 'ledger-fix2.jsonl'
P = R.T / 'prof'
IMG = 'localhost/sentinel-cv:v8-t2-fix2f'
tmpl = next(x for x in json.loads((R.T / 'plan.json.main-run').read_text()) if x.get('kind') == 'frontier')['template']
tmpl = R.set_arg(R.set_arg(tmpl, '--min-free-disk-mib', '3072'), '--image', IMG)
PEOPLE = lambda n: [f'tcp://192.168.1.92:{19101 + i}' for i in range(n)]
FX = R.T / 'fixtures'
OLD = ['SENTINEL_FACE_PLAIN=0', 'SENTINEL_GAIT_PLAIN=0']   # T2 face/gait graph chain = same work as measured T2
Q4 = ['SENTINEL_T2_POSTDET_QUEUE=4']
LAT = ['SENTINEL_GVA_SCHEDULING_POLICY=latency']

def case(name, n, prof, envs, recording=None, steady=None, sample=True):
    if name in R.done_cases(): return R.done_cases()[name]
    c = R.set_arg(tmpl, '--case', name)
    c = R.set_arg(R.set_arg(R.set_arg(c, '--cameras', str(n)), '--fps', '8'), '--profile', prof)
    c = R.set_arg(c, '--sample-seconds', '5')
    if recording:
        i = c.index('--source-urls-json'); c = c[:i] + ['--recording', str(recording)] + c[i + 2:]
    else:
        c = R.set_arg(c, '--source-urls-json', json.dumps(PEOPLE(n)))
    if steady: c = R.set_arg(c, '--steady-seconds', str(steady))
    for e in envs: c = c + ['--container-env', e]
    smp = subprocess.Popen(['python3', str(P / 'host_sampler.py'), str(P / name)], start_new_session=True) if sample else None
    try:
        return R.run_case(name, c, extra=dict(group='fix2', profile=prof, fps=8, cameras=n, envs=envs, image=IMG,
                                              recording=str(recording) if recording else None))
    finally:
        if smp:
            try: os.killpg(smp.pid, signal.SIGINT); smp.wait(timeout=10)
            except Exception:
                try: os.killpg(smp.pid, signal.SIGKILL)
                except Exception: pass
        subprocess.run(['chown', '-R', '1000:1000', str(P), str(R.T / 'artifacts')])

R.status(state='FIX2F_STARTING')
case('f2f-S-8x8-grid', 8, 'S', [])   # FIX2e: grid pixels + advance after sample (compare f2c-S-8x8-grid, f2-S-8x8-slots)
R.status(state='FIX2F_DONE')
