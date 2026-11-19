#!/usr/bin/env python3
"""T2-FIX2 tests on .64 (root). Same guarded harness/criteria/isolation as T2 campaign; own ledger; each case once.
Queued in HISTORY_CHECK_2026-11-17.md (Waiting 1-3 + plain face/gait capacity)."""
import json, sys, subprocess, signal, os
sys.argv = sys.argv[:1]
import runner as R
R.LEDGER = R.T / 'ledger-fix2.jsonl'
P = R.T / 'prof'
IMG = 'localhost/sentinel-cv:v8-t2-fix2'
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
R.status(state='FIX2_STARTING')
# 1. capacity: queue after inference (+ scheduling) with the SAME face/gait work as T2 (T2 SA 5x8 = fail, 6.9 FPS)
r1 = case('f2-SA-5x8-old-q4', 5, 'SA', OLD + Q4)
r2 = case('f2-SA-5x8-old-q4-lat', 5, 'SA', OLD + Q4 + LAT)
# 2. capacity cost of plain face + plain gait (defaults of the fix2 image); T2 SA 4x8 passes 5 of 6 clean runs
r3 = case('f2-SA-4x8-plain', 4, 'SA', [])
best = OLD + Q4 + LAT if ok(r2) else (OLD + Q4 if ok(r1) else None)
if best is not None:
    case('f2-SA-6x8-best', 6, 'SA', best)
# 3. plate recovery + plate width (1 camera, recordings, profile SV = same as step1 plate-gate runs)
for vid, tag in ((FX / 'ANPR.mp4', 'anpr'), (FX / 'traffic1.mkv', 'traffic1')):
    case(f'f2-SV-{tag}-rec0', 1, 'SV', [], vid, sample=False)
    case(f'f2-SV-{tag}-rec1', 1, 'SV', ['SENTINEL_PLATE_RECOVERY=1'], vid, sample=False)
    case(f'f2-SV-{tag}-w60', 1, 'SV', ['PLATE_MIN_WIDTH=60'], vid, sample=False)
# 4. scene slots: 8 scene-only cameras (scene_sample times per camera)
case('f2-S-8x8-slots', 8, 'S', [])
R.status(state='FIX2_DONE')
