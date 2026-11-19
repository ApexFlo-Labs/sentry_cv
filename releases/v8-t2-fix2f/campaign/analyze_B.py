"""Phase B analysis: per-element time, GPU engines, NPU, per-thread CPU, per-camera FPS, for SA 1..6 cams x 8 FPS."""
import json, glob, re, collections, statistics as st, sys
out = {}
for n in range(1, 7):
    name = f'prof-B-SA-{n}x8-traced'
    raw = glob.glob(f'data/runs/{name}/raw-*')[0]
    rep = json.load(open(raw + '/report.json'))
    pc = rep['steady_throughput']['per_camera']
    t0, t1 = rep.get('steady_started_monotonic'), rep.get('steady_ended_monotonic')
    # element latency (steady part not separable by monotonic; use 2nd half of log lines)
    lat = collections.defaultdict(list)
    lines = open(raw + '/delivery/gst-latency.log', errors='ignore').readlines()
    for l in lines[len(lines) // 3:]:
        if 'element-latency' not in l: continue
        m = re.search(r'element=\(string\)([^,]+).*?time=\(guint64\)(\d+)', l)
        if m: lat[re.sub(r'\d+$', '', m.group(1)) if not m.group(1).startswith(('queue','capsfilter')) else m.group(1)].append(int(m.group(2)) / 1e6)
    # host samples
    hs = [json.loads(l) for l in open(f'data/prof/{name}/host_samples.jsonl')]
    hs = hs[len(hs) // 3:]  # skip startup
    sent = collections.Counter(); thr = collections.defaultdict(list)
    for s in hs:
        if s['top_procs']: sent[s['top_procs'][0]['pid']] += 1
    pid = sent.most_common(1)[0][0]
    for s in hs:
        for t in s['top_threads']:
            if t['pid'] == pid: thr[t['name']].append(t['cpu_pct'])
    procpct = [sum(t['cpu_pct'] for t in s['top_threads'] if t['pid'] == pid) for s in hs]
    # gpu engines
    txt = open(f'data/prof/{name}/gpu_top.json').read(); dec = json.JSONDecoder(); g = []; i = txt.find('{')
    while i != -1:
        try: o, j = dec.raw_decode(txt, i); g.append(o); i = txt.find('{', j)
        except Exception: break
    g = g[len(g) // 3:]
    eng = collections.defaultdict(list)
    for s in g:
        for k, v in (s.get('engines') or {}).items(): eng[k].append(v.get('busy', 0))
    freq = [s.get('frequency', {}).get('actual', 0) for s in g]
    out[n] = dict(passed=rep['passed'], cam_fps=sorted(round(v['selected_fps'], 2) for v in pc.values()),
                  min_window=round(min(v['minimum_window_ratio'] for v in pc.values()), 3),
                  elements={k: dict(n=len(v), mean=round(st.mean(v), 2), p95=round(sorted(v)[int(.95 * len(v))], 2)) for k, v in lat.items() if len(v) > 20},
                  gpu_engines={k: dict(mean=round(st.mean(v), 1), max=round(max(v), 1)) for k, v in eng.items()},
                  gpu_mhz=round(st.mean(freq)), npu_pct=round(st.mean(s['npu_pct'] for s in hs), 1), npu_max=max(s['npu_pct'] for s in hs),
                  sys_cpu_pct=round(st.mean(s['sys_cpu_pct'] for s in hs), 1),
                  sentinel_proc_cpu_pct=round(st.mean(procpct)),
                  threads={k: dict(mean=round(sum(v) / len(hs), 1), max=max(v)) for k, v in sorted(thr.items(), key=lambda kv: -sum(kv[1]))[:18]})
json.dump(out, open('phaseB_summary.json', 'w'), indent=1)
for n, r in out.items():
    print(f"\n===== {n} cams: {'PASS' if r['passed'] else 'FAIL'} fps={r['cam_fps']} minwin={r['min_window']}")
    print(' GPU engines', {k: v['mean'] for k, v in r['gpu_engines'].items()}, 'MHz', r['gpu_mhz'], '| NPU', r['npu_pct'], 'max', r['npu_max'], '| sysCPU%', r['sys_cpu_pct'], '| sentinel proc CPU%', r['sentinel_proc_cpu_pct'])
    print(' top threads', [(k, v['mean'], v['max']) for k, v in list(r['threads'].items())[:12]])
    print(' slowest elements', sorted([(k, v['mean'], v['p95']) for k, v in r['elements'].items()], key=lambda x: -x[1])[:12])
