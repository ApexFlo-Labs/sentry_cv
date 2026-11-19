#!/usr/bin/env python3
"""T2-FIX2g: final-config limit + attribution on .64 (root). Same guarded harness/criteria/isolation as T2 campaign; own ledger; each case once.
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

ok = lambda r: bool(r and r.get('passed'))
R.status(state='FIX2G_STARTING')
r6 = case('f2f-SA-6x8-plain-q4-lat', 6, 'SA', Q4 + LAT)   # next point of the final config (5x8 passed)
case('f2f-SA-5x8-plain', 5, 'SA', [])                      # attribution: plain face/gait alone, no queue/latency
if ok(r6):
    case('f2f-SA-7x8-plain-q4-lat', 7, 'SA', Q4 + LAT)
R.status(state='FIX2G_DONE')
