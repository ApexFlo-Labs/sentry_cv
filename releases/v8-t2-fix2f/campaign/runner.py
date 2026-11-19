#!/usr/bin/env python3
"""T1 campaign runner on .64. Runs as root. Same harness/criteria as C18; ledger.jsonl after every case."""
import json, os, subprocess, sys, time, glob, shutil, signal
from pathlib import Path
T = Path('/home/admin1/Documents/sentry-validation-20261116-t2')
REPO = T / 'repo'
IMG = 'sha256:14978d1b8f9047be2d5817013ea4fe149e3fd5be8f3c15f61aaf384b39045dc3'
RUN_ID = 'remote64-t2'
LEDGER = T / 'ledger-b.jsonl'
STATUS = T / 'STATUS.json'
plan = json.loads((T / 'plan.json').read_text())
only = set(sys.argv[1].split(',')) if len(sys.argv) > 1 and sys.argv[1] != 'all' else None
env = dict(os.environ, PYTHONUNBUFFERED='1')

def now(): return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def status(**kw): STATUS.write_text(json.dumps(dict(utc=now(), **kw), indent=1))
def done_cases():
    if not LEDGER.exists(): return {}
    return {r['case']: r for r in map(json.loads, LEDGER.read_text().splitlines())}
def ledger(row):
    with LEDGER.open('a') as f: f.write(json.dumps(row) + '\n')
    subprocess.run(['chown', '1000:1000', str(LEDGER)])

def set_arg(c, flag, value):
    c = list(c); c[c.index(flag)+1] = value; return c

def find_report(case, command):
    if 'v8_recovery_image_validation.py' in ' '.join(command):
        out = command[command.index('--output')+1]
        p = Path(out) / 'report.json'
        return p if p.exists() else None
    arch = T / 'artifacts/runs' / RUN_ID / case
    raws = []
    for log in sorted(arch.glob('attempt-*.stdout.log'), key=lambda p: p.stat().st_mtime):
        for line in log.read_text(errors='replace').splitlines():
            try: raw = json.loads(line).get('artifact_directory')
            except Exception: continue
            if raw: raws.append(Path(raw))
    for raw in raws[-1:]:
        dest = arch / ('raw-' + raw.name)
        if raw.exists() and not dest.exists():
            shutil.copytree(raw, dest, ignore=lambda d, names: [n for n in names if Path(d, n).is_socket()])
            shutil.rmtree(raw, ignore_errors=True)
        if (dest / 'report.json').exists(): return dest / 'report.json'
    return None

def summarize(report_path):
    r = json.loads(report_path.read_text())
    s = dict(report=str(report_path), passed=r.get('passed'))
    for k in ('steady_completed_work', 'steady_throughput'):
        v = r.get(k) or {}
        pc = v.get('per_camera') or {}
        s[k] = dict(passed=v.get('passed'),
                    fps={c: round(x.get('selected_fps', 0), 3) for c, x in pc.items()},
                    min_window=min([x.get('minimum_window_ratio', 1) for x in pc.values()] or [None]))
    s['events'] = (r.get('package_summary') or {}).get('event_types')
    cov = (r.get('steady_output_coverage') or {})
    s['coverage_passed'] = cov.get('passed')
    s['coverage_errors'] = {c: x.get('errors') for c, x in (cov.get('per_camera') or {}).items() if x.get('errors')}
    s['embeddings'] = {c: x.get('embedding_profiles') for c, x in (cov.get('per_camera') or {}).items()}
    s['fatal_lines'] = (r.get('fatal_lines') or [])[:5]
    s['utc_sanity'] = (r.get('source_utc_sanity') or {}).get('passed')
    tot = {}
    for c, x in ((r.get('end_health') or {}).get('per_camera') or {}).items():
        t = x.get('totals') or {}
        tot[c] = {k: t[k] for k in t if k.startswith('anpr_plate_gate') or k in ('decoded_frames', 'inference_frames')}
    s['gate_counters'] = tot
    for k in ('passed', 'gil_blocking_parent_recovery_verified', 'recovery_verified'):
        if k in r and k not in s: s[k] = r[k]
    return s

def isolation_state():
    q = lambda u: subprocess.run(['systemctl', 'is-active', u], capture_output=True, text=True).stdout.strip()
    shims = subprocess.run(['pgrep', '-c', '-f', 'containerd-shim'], capture_output=True, text=True).stdout.strip()
    return dict(no_turbo=open('/sys/devices/system/cpu/intel_pstate/no_turbo').read().strip(), k3s=q('k3s'),
                apexfabric_control=q('apexfabric-control'), containerd_shims=shims)

def run_case(name, command, timeout=21000, extra=None):
    iso = isolation_state()
    if iso['no_turbo'] != '1' or iso['k3s'] == 'active' or iso['apexfabric_control'] == 'active':
        ledger(dict(case=name, utc=now(), passed=None, skipped='ISOLATION_LOST', isolation=iso)); status(state='STOPPED_ISOLATION_LOST', iso=iso)
        raise SystemExit('isolation lost: ' + json.dumps(iso))
    extra = dict(extra or {}, isolation_before=iso)
    out = T / 'artifacts' / 'logs'; out.mkdir(parents=True, exist_ok=True)
    status(state='RUNNING', case=name)
    t0 = time.time()
    with (out / (name + '.log')).open('w') as log:
        p = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try: rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM); rc = 'timeout'
    row = dict(case=name, utc=now(), exit=rc, seconds=round(time.time()-t0), **(extra or {}))
    rp = find_report(name, command)
    if rp is None:  # unguarded or suite: look for artifact_directory in log
        for line in (out / (name + '.log')).read_text(errors='replace').splitlines()[::-1]:
            if line.startswith('{') and 'artifact_directory' in line:
                try: rp = Path(json.loads(line)['artifact_directory']) / 'report.json'
                except Exception: pass
                break
    if rp is not None and rp.exists():
        try: row.update(summarize(rp))
        except Exception as e: row['summary_error'] = repr(e)
    else:
        row['passed'] = (rc == 0)
    ledger(row)
    return row

def fresh(d):
    if d.exists(): d.rename(d.with_name(f'stale-{d.name}-{int(time.time())}'))
    return d

def wait_port(port, secs=20):
    import socket
    for _ in range(secs*10):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=.5).close(); return True
        except OSError: time.sleep(.1)
    return False

def server(kind):
    if kind == 'http':
        fresh(T/'artifacts/http-source')
        p = subprocess.Popen(['taskset','-c','14,15','python3','scripts/isolated_validation/http_replay_source.py','--fixture',
            str(T/'fixtures/people1080p30.mp4'),'--output',str(T/'artifacts/http-source'),'--port','18880','--seconds','7200'],
            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
        if not wait_port(18880): raise RuntimeError('http source did not start')
        return p
    if False:
        return subprocess.Popen(['taskset','-c','14,15','python3','scripts/isolated_validation/http_replay_source.py','--fixture',
            str(T/'fixtures/people1080p30.mp4'),'--output',str(T/'artifacts/http-source'),'--port','18880','--seconds','7200'],
            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
    if kind == 'tcp':
        o = fresh(T/'artifacts/tcp-multiclient-source')
        p = subprocess.Popen(['taskset','-c','14,15','python3','scripts/isolated_validation/tcp_replay_source.py','--fixture',
            str(T/'fixtures/people1080p30.mp4'),'--output',str(o),'--port','18881','--seconds','7200'],
            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(100):
            if (o/'ready.json').exists(): break
            time.sleep(.1)
        return p
    return None

def frontier(e, done):
    prof, fps = e['profile'], e['fps']
    def point(n):
        name = f'frontier-{prof}-{n}x{fps}'
        if name in done: return done[name]
        c = set_arg(e['template'], '--case', name)
        c = set_arg(set_arg(set_arg(c, '--cameras', str(n)), '--fps', str(fps)), '--profile', prof)
        c = set_arg(c, '--source-urls-json', json.dumps([f'tcp://192.168.1.92:{19101+i}' for i in range(n)]))
        return run_case(name, c, extra=dict(group='frontier', profile=prof, fps=fps, cameras=n,
                        c18_verdict='PASS' if n <= e['c18_pass'] else 'FAIL'))
    ok = lambda r: bool(r.get('passed'))
    n = e['c18_pass']
    r = point(n)
    if ok(r):
        n += 1
        while ok(point(n)) and n < e['c18_pass'] + 6: n += 1
        best = n - 1
    else:
        n -= 1
        while n >= 1 and not ok(point(n)): n -= 1
        best = n
    return best

def main():
    done = done_cases()
    best = {}
    status(state='STARTING')
    for e in plan:
        if only and e['group'] not in only and e.get('name') not in only: continue
        kind = e.get('kind')
        if kind == 'frontier':
            best[(e['profile'], e['fps'])] = frontier(e, done_cases()); continue
        if kind == 'hour':
            n = best.get(('SA', 8)) or 3
            name = f"{e['name']}-{n}cam"
            if name in done: continue
            c = set_arg(e['template'], '--case', name)
            c = set_arg(set_arg(c, '--cameras', str(n)), '--source-urls-json',
                        json.dumps([f'tcp://192.168.1.92:{19101+i}' for i in range(n)]))
            run_case(name, c, extra=dict(group='hour', c18=e['c18'], cameras=n)); continue
        name = e['name']
        if name in done: continue
        if e.get('needs_baseline'):
            base = next(x for x in plan if x.get('name') == 'baseline-SA-1x5-people')
            (T/'artifacts/baseline-command.json').write_text(json.dumps({'command': base['command'], 'image': IMG}))
        srv = server(e.get('server')) if e.get('server') in ('http', 'tcp') else None
        try:
            if e.get('server') == 'https':
                sys.path.insert(0, str(REPO))
                from scripts.isolated_validation.https_fixture import trusted_https_fixture
                with trusted_https_fixture(T, fresh(T/'artifacts/https-source')) as (url, hook):
                    c = set_arg(set_arg(e['command'], '--source-url', url), '--diagnostic-hook', str(hook))
                    run_case(name, c, extra=dict(group=e['group'], c18=e.get('c18')))
            else:
                run_case(name, e['command'], extra=dict(group=e['group'], c18=e.get('c18')))
        finally:
            if srv: srv.terminate(); srv.wait(timeout=20)
    status(state='DONE', best_frontiers={f'{k[0]}x{k[1]}': v for k, v in best.items()})

if __name__ == '__main__':
    main()
