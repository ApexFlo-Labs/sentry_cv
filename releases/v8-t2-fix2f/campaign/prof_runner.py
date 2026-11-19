#!/usr/bin/env python3
"""T2 profiling series on .64 (root). Reuses runner.py's guarded run_case unchanged, own ledger.
Phase B: all apps (SA) at 8 FPS, 1..6 cameras, GStreamer per-element latency tracer + 1 s host sampler.
Phase C: each use-case profile alone at 8 FPS, find the most cameras that pass (no tracer, host sampler on).
usage: prof_runner.py B|C|BC"""
import json, sys, subprocess, signal, time
sys.argv, phases = sys.argv[:1], (sys.argv[1] if len(sys.argv) > 1 else 'BC')
import runner as R
R.LEDGER = R.T / 'ledger-prof.jsonl'
P = R.T / 'prof'; P.mkdir(exist_ok=True)
tmpl = next(x for x in json.loads((R.T / 'plan.json.main-run').read_text()) if x.get('kind') == 'frontier')['template']
HOOK = str(R.T / 'repo/scripts/isolated_validation/gst_timing_diagnostic_hook.py')
TRACE = ['--diagnostic-hook', HOOK, '--container-env', 'GST_TRACERS=latency(flags=pipeline+element+reported)',
         '--container-env', 'GST_DEBUG=GST_TRACER:7', '--container-env', 'GST_DEBUG_NO_COLOR=1',
         '--container-env', 'GST_DEBUG_FILE=/run/sentinel-delivery/gst-latency.log']
PEOPLE = lambda n: [f'tcp://192.168.1.92:{19101 + i}' for i in range(n)]
VEH = lambda n: [f'tcp://192.168.1.92:{19401 + i}' for i in range(n)]

def case(name, n, prof, srcs, fps=8, trace=False, extra=None):
    done = R.done_cases()
    if name in done: return done[name]
    c = R.set_arg(tmpl, '--case', name)
    c = R.set_arg(R.set_arg(R.set_arg(c, '--cameras', str(n)), '--fps', str(fps)), '--profile', prof)
    c = R.set_arg(R.set_arg(c, '--source-urls-json', json.dumps(srcs)), '--sample-seconds', '5')
    if trace: c = c + TRACE
    smp = subprocess.Popen(['python3', str(P / 'host_sampler.py'), str(P / name)], start_new_session=True)
    try:
        return R.run_case(name, c, extra=dict(group='prof', profile=prof, fps=fps, cameras=n, traced=trace, **(extra or {})))
    finally:
        smp.send_signal(signal.SIGINT); smp.wait(timeout=20)
        subprocess.run(['chown', '-R', '1000:1000', str(P)])

def frontier(prof, srcs_fn, start, cap, fps=8):
    ok = lambda r: bool(r.get('passed'))
    name = lambda k: f'prof-C-{prof}-{k}x{fps}'
    n = start
    if ok(case(name(n), n, prof, srcs_fn(n), fps)):
        while n < cap and ok(case(name(n + 1), n + 1, prof, srcs_fn(n + 1), fps)): n += 1
        best = n
    else:
        n -= 1
        while n >= 1 and not ok(case(name(n), n, prof, srcs_fn(n), fps)): n -= 1
        best = n
    R.ledger(dict(case=f'prof-C-{prof}-best', utc=R.now(), profile=prof, fps=fps, best=best, cap_hit=best >= cap))
    return best

R.status(state='PROF_STARTING', phases=phases)
if 'B' in phases:
    for n in range(1, 7):
        case(f'prof-B-SA-{n}x8-traced', n, 'SA', PEOPLE(n), trace=True)
if 'C' in phases:
    # people-side profiles on the people video, vehicle-side on the ANPR video, fire on people video (no fire -> model cost only)
    for prof, src, start, cap in [('SP', PEOPLE, 12, 30), ('SC', PEOPLE, 12, 30), ('SX', PEOPLE, 10, 30),
                                  ('SH', PEOPLE, 12, 30), ('SIB', PEOPLE, 6, 20), ('SIF', PEOPLE, 6, 20),
                                  ('SIFB', PEOPLE, 5, 20), ('SIFBG', PEOPLE, 4, 20),
                                  ('SV', VEH, 6, 30), ('SVX', VEH, 6, 30)]:
        frontier(prof, src, start, cap)
R.status(state='PROF_DONE', phases=phases)
