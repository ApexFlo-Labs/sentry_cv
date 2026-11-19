#!/usr/bin/env python3
"""Phase D on .64 (root): settings-only fixes for the found bottleneck. Same guarded harness/ledger as prof_runner."""
import json, sys, subprocess, signal
sys.argv = sys.argv[:1]
import runner as R
R.LEDGER = R.T / 'ledger-prof.jsonl'
P = R.T / 'prof'
tmpl = next(x for x in json.loads((R.T / 'plan.json.main-run').read_text()) if x.get('kind') == 'frontier')['template']
PEOPLE = lambda n: [f'tcp://192.168.1.92:{19101 + i}' for i in range(n)]
FIX1 = 'sha256:cf289af8fae7b801138e0b9dfc01fea2b4bbf5248eab2362b267ff34d0b4cb15'
def case(name, n, prof, envs, image=None):
    if name in R.done_cases(): return
    c = R.set_arg(tmpl, '--case', name)
    c = R.set_arg(R.set_arg(R.set_arg(c, '--cameras', str(n)), '--fps', '8'), '--profile', prof)
    c = R.set_arg(R.set_arg(c, '--source-urls-json', json.dumps(PEOPLE(n))), '--sample-seconds', '5')
    if image: c = R.set_arg(c, '--image', image)
    for e in envs: c = c + ['--container-env', e]
    smp = subprocess.Popen(['python3', str(P / 'host_sampler.py'), str(P / name)], start_new_session=True)
    try: R.run_case(name, c, extra=dict(group='fix', profile=prof, fps=8, cameras=n, envs=envs, image=image or 'T2'))
    finally:
        import os
        try: os.killpg(smp.pid, signal.SIGINT); smp.wait(timeout=10)
        except Exception:
            try: os.killpg(smp.pid, signal.SIGKILL)
            except Exception: pass
        subprocess.run(['chown', '-R', '1000:1000', str(P)])
T4 = ['SENTINEL_T2_CONVERT_THREADS=4']
NPU = ['SCENE_EMBEDDER_DEVICE=NPU']
R.status(state='FIX_STARTING')
S1 = ['SENTINEL_MODEL_SHARD_SIZE=1']
case('fix1-SA-5x8-shard1-conv4', 5, 'SA', S1 + T4, FIX1)
case('fix1-SA-6x8-shard1-conv4', 6, 'SA', S1 + T4, FIX1)
case('fix1-SA-5x8-shard1', 5, 'SA', S1, FIX1)
case('fix-SA-6x8-conv4', 6, 'SA', T4)
case('fix1-SA-6x8-shard1-conv4-sceneNPU', 6, 'SA', S1 + T4 + NPU, FIX1)
case('fix-SP-11x8-sceneNPU', 11, 'SP', NPU)
R.status(state='FIX_DONE')
