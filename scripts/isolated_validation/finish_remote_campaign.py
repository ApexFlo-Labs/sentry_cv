#!/usr/bin/env python3
"""Run image regression/HTTP/independent-latency checks after the native batch.

Never overlaps a capacity case, silently reuses another image's result, changes
power/policy, publishes, or claims that these checks close the whole catalog.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2)+'\n')
    temporary.replace(path)
    os.chown(path, 1000, 1000)


def terminal_guard_attempt(verdict):
    """Validate the actual guard wire format, including final cleanup proof."""
    assert verdict.get('status') in ['PASS', 'FAIL'], 'Guard excluded or incomplete'
    assert 'supervisor_exit_code' in verdict, 'Guard has not terminated'
    attempt = verdict.get('attempt')
    assert isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0
    matches = [row for row in verdict.get('attempts', []) if row.get('attempt') == attempt]
    assert len(matches) == 1, 'Missing or duplicate final attempt'
    assert matches[0].get('cleanup_ok') is True, 'Final cleanup not confirmed'
    assert matches[0].get('result') == verdict['status'], 'Attempt and supervisor verdict differ'
    return attempt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--run-id', default='remote64-c13')
    p.add_argument('--skip-verified-baked-checks', action='store_true')
    p.add_argument('--continue-independent-on-failure',action='store_true',
                   help='Record failing independent phases and run the others; never converts FAIL to PASS')
    a = p.parse_args()
    r = a.root.resolve()
    import sys
    sys.path.insert(0, str(r/'repo'))
    # Hold for the whole controller lifetime. A second helper must not replace
    # status while a predecessor is still terminating or operating a case.
    lease = (r/'POSTCHECKS.lock').open('a')
    fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    env['PATH'] = '/home/admin1/sentry_cv/.venv/bin:'+env['PATH']
    status_path = r/'POSTCHECKS_CURRENT.json'
    stop = [False]
    child = [None]
    phases = []

    def interrupt(*_):
        stop[0] = True
        if child[0] is not None and child[0].poll() is None:
            child[0].terminate()
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, interrupt)

    def status(phase, **extra):
        value = {'utc': datetime.now(timezone.utc).isoformat(),
             'pid': os.getpid(), 'status': phase, 'image': a.image,
             'capacity_qualified': False, **extra}
        save(status_path, value)
        previous = json.loads((r/'CAMPAIGN_CURRENT.json').read_text())
        if phase != 'WAITING_NATIVE_BATCH' and (previous.get('stage') == 'postchecks'
                or previous.get('status') == 'PLANNED_NATIVE_BATCH_FINISHED_REQUIRES_EVIDENCE_REVIEW'):
            save(r/'CAMPAIGN_CURRENT.json', {**previous, **value,
                 'controller_pid': os.getpid(), 'stage': 'postchecks'})

    def safety():
        assert not stop[0] and not (r/'STOP').exists(), 'User stop'
        assert Path('/sys/devices/system/cpu/intel_pstate/no_turbo').read_text().strip() == '1'
        for unit in ['k3s.service', 'apexfabric-control.service']:
            assert subprocess.run(['systemctl', 'is-active', unit], capture_output=True).returncode != 0

    @contextmanager
    def phase(name):
        safety()
        status('RUNNING',case=name)
        try:
            yield
        except Exception as exc:
            phases.append({'phase':name,'status':'FAIL','reason':type(exc).__name__+': '+str(exc)})
            save(r/'artifacts/independent-phase-results.json',phases)
            if stop[0] or (r/'STOP').exists() or not a.continue_independent_on_failure:
                raise
            # Never overlap another case with a failed cleanup. Model-specific
            # assertions may fail independently; live owned containers may not.
            owned=subprocess.check_output(['docker','ps','-q','--filter',
                   'label=sentinel.validation_run='+a.run_id],text=True).strip()
            assert not owned,'Owned test container still active after failed phase; cleanup required'
        else:
            phases.append({'phase':name,'status':'PASS'})
            save(r/'artifacts/independent-phase-results.json',phases)

    def run(name, command, timeout=5400):
        safety()
        output = r/'artifacts'/('post-'+name)
        output.mkdir(exist_ok=False)
        save(output/'command.json', {'command': command, 'image': a.image})
        status('RUNNING', case=name)
        with (output/'launch.log').open('w') as log:
            child[0] = subprocess.Popen(command, cwd=r/'repo', env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = child[0].wait(timeout=timeout)
            except BaseException:
                child[0].terminate()
                raise
        child[0] = None
        save(output/'exit.json', {'exit_code': rc})
        assert rc == 0, name+' failed; inspect retained evidence'

    def native(name, arguments, *, cameras=1, fps=8, steady_seconds=120,
               allow_capacity_failure=False, gallery_limit=128,
               gallery_interval_seconds=10):
        case = 'post-'+name
        safety()
        command = ['taskset','-c','14,15','python3',
                   'scripts/isolated_validation/run_guarded_case.py',
                   '--root',str(r/'artifacts/runs'),'--run-id',a.run_id,
                   '--case',case,'--engine','docker','--max-attempts','3',
                   '--sample-interval','.5','--hot-samples','9',
                   '--min-free-disk-mib','6144','--min-available-ram-mib','4096',
                   '--max-runtime','1500','--max-cooldown','6000','--',
                   'taskset','-c','0-15','python3','scripts/v8_capacity_probe.py',
                   '--engine','docker','--image',a.image,'--cameras',str(cameras),'--fps',str(fps),
                   '--port','18380','--startup-timeout','360','--warmup-seconds','20',
                   '--steady-seconds',str(steady_seconds),'--sample-seconds','10','--activity','active',
                   '--memory-limit-mib','24576','--capture-gallery-limit',str(gallery_limit),
                   '--capture-gallery-interval-seconds',str(gallery_interval_seconds),*arguments]
        save(r/'artifacts'/(case+'-command.json'), {'command': command})
        bridge = None
        try:
            status('RUNNING', case=case)
            with (r/'artifacts'/(case+'-controller.log')).open('w') as log:
                child[0] = subprocess.Popen(command,cwd=r/'repo',env=env,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                bridge = subprocess.Popen(['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/remote_case_bridge.py','--root',str(r),
                    '--run-id',a.run_id,'--case',case,'--guard-pid',str(child[0].pid),
                    '--owner','1000','--seconds','10800'],cwd=r/'repo',env=env,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                rc = child[0].wait(timeout=21000)
                bridge.wait(timeout=120)
            child[0] = None
            archived = r/'artifacts/runs'/a.run_id/case
            verdict = json.loads((archived/'status.json').read_text())
            attempt = terminal_guard_attempt(verdict)
            stdout = archived/('attempt-'+str(attempt)+'.stdout.log')
            report_path = None
            for line in stdout.read_text().splitlines():
                try:
                    raw = json.loads(line).get('artifact_directory')
                except (ValueError, AttributeError):
                    continue
                if raw and Path(raw).parent == Path('/tmp'):
                    report_path = archived/('raw-'+Path(raw).name)/'report.json'
            assert report_path is not None and report_path.is_file(), 'Missing final-attempt archive'
            report = json.loads(report_path.read_text())
            assert report['image_config_id'] == a.image, 'Wrong native image'
            passed = bool(report['passed']) and rc == 0
            ledger = r/'artifacts/continuation-ledger.json'
            rows = json.loads(ledger.read_text())
            rows.append({'case': case, 'status': 'PASS' if passed else 'FAIL', 'probe_passed': passed,
                         'scope': ('Fixed-fleet role-budget short search; not sustained qualification'
                                   if allow_capacity_failure else 'Post-batch functional/latency test; not capacity'),
                         'report_path': str(report_path), 'capacity_qualified': False,
                         **{k: report.get(k) for k in ['source_mode', 'package_summary',
                           'steady_throughput', 'steady_completed_work',
                           'steady_output_coverage', 'source_utc_sanity', 'fatal_lines']}})
            save(ledger, rows)
            assert passed or allow_capacity_failure, 'Native probe failed: '+case
            if allow_capacity_failure:
                assert report.get('final_health_available'), 'Missing final native health: '+case
                assert not report.get('fatal_lines'), 'Native error is not an ordinary capacity boundary: '+case
                assert report.get('source_utc_sanity', {}).get('passed'), 'Timestamp failure: '+case
            return report_path.parent
        finally:
            if child[0] is not None and child[0].poll() is None:
                child[0].terminate()
            if bridge is not None and bridge.poll() is None:
                bridge.terminate()

    status('WAITING_NATIVE_BATCH')
    try:
        while True:
            safety()
            state = json.loads((r/'CAMPAIGN_CURRENT.json').read_text())
            assert state['image'] == a.image
            assert state['status'] not in ['NEEDS_REVIEW','STOPPED'], 'Native controller needs review'
            if state['status'] == 'PLANNED_NATIVE_BATCH_FINISHED_REQUIRES_EVIDENCE_REVIEW':
                frontier_path = r/'artifacts/continuation-frontier.json'
                frontier = json.loads(frontier_path.read_text())
                if any(v.get('first_short_failing_count') is None
                       and v.get('last_short_passing_count', 0) < 50
                       for v in frontier.values()):
                    # The original live controller may have loaded an older
                    # bounded search plan. Resume using the updated search,
                    # preserving exact-command checks and all original rows.
                    launch = json.loads((r/'artifacts/continuation-resume-20261112.json').read_text())
                    command = launch['command']
                    assert '--resume' in command and a.image in command
                    run('extend-capacity-search', command, timeout=172800)
                    continue
                ledger = json.loads((r/'artifacts/continuation-ledger.json').read_text())
                holds = [row for row in ledger if row['case'].endswith('one-hour')]
                hour_passed=bool(holds and holds[-1]['status']=='PASS' and holds[-1]['probe_passed'])
                if not hour_passed:
                    assert a.continue_independent_on_failure, 'Hour qualification did not pass'
                    phases.append({'phase':'preceding-one-hour','status':'FAIL_OR_NOT_RUN',
                                   'reason':'Independent checks continue; sustained capacity remains unqualified'})
                    save(r/'artifacts/independent-phase-results.json',phases)
                break
            time.sleep(5)
        safety()
        owned = subprocess.check_output(['docker','ps','-q','--filter',
                   'label=sentinel.validation_run='+a.run_id],text=True).strip()
        assert not owned, 'Capacity container still running'
        if a.skip_verified_baked_checks:
            proof = json.loads((r/'artifacts/baked-checks/report.json').read_text())
            assert proof['result'] == 'passed' and proof['image']['config_id'] == a.image
        else:
            run('baked-checks',['taskset','-c','0-15','python3',
                'scripts/isolated_validation/run_baked_checks.py','--engine','docker',
                '--image',a.image,'--output',str(r/'artifacts/baked-checks')])
        with phase('bad-first-source-fresh-baseline-counter-r2'):
            case='post-bad-first-source-SA8-baseline-counter-r2'
            output=r/'artifacts/recovery-bad-first-source-SA8-baseline-counter-r2'
            try:
                run('bad-first-source-SA8-baseline-counter-r2',[
                    'taskset','-c','14,15','python3',
                    'scripts/isolated_validation/run_guarded_case.py',
                    '--root',str(r/'artifacts/runs'),'--run-id',a.run_id,
                    '--case',case,'--engine','docker','--max-attempts','3',
                    '--sample-interval','.5','--hot-samples','9',
                    '--min-free-disk-mib','6144','--min-available-ram-mib','4096',
                    '--max-runtime','900','--max-cooldown','6000','--',
                    'taskset','-c','0-15','python3',
                    'scripts/isolated_validation/v8_recovery_image_validation.py',
                    '--engine','docker','--image',a.image,'--mode','bad-first-source',
                    '--profile','SA','--fps','8','--port','18381',
                    '--upstream-url','rtsps://192.168.1.92:18554/recovery',
                    '--upstream-ca-file',str(r/'fixture-source-ca.crt'),
                    '--output',str(output)],timeout=21000)
            finally:
                for directory, dirs, files in os.walk(output):
                    os.chown(directory,1000,1000)
                    for filename in files:
                        path=Path(directory)/filename
                        if not path.is_socket():os.chown(path,1000,1000)
        with phase('tls-regressions'):
            run('tls-regressions',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/camera_tls_qualification.py',
                '--engine','docker','--image',a.image,
                '--output',str(r/'artifacts/tls-regressions')],timeout=600)
        with phase('agent-api'):
            api = r/'artifacts/agent-api-regressions'
            api.mkdir(mode=0o700)
            os.chown(api,10001,10001)
            try:
                run('agent-api-regressions',['docker','run','--rm','--pull=never','--network=none',
                    '--label','sentinel.validation_run='+a.run_id,
                    '--label','sentinel.validation_case=post-agent-api-regressions',
                    '--cpus','.5','--memory','2g','--entrypoint','python3',
                    '-e','PYTHONPATH=/test-support:/opt/traffic-pilot:/opt/traffic-pilot/services/worker',
                    '-v',str(r/'repo/scripts/isolated_validation')+':/test-support:ro',
                    '-v',str(api)+':/results:rw',a.image,'/test-support/v8_agent_api_fault_probe.py',
                    '--cases','normal-durable','retry-429-503','disconnect-before-store',
                    'disconnect-after-store','reject-400','wrong-package-ack','missing-evidence-ack',
                    'exhaust-503','reject-401','agent-absent','slow-first-response'],timeout=600)
            finally:
                # Failed API evidence is just as important to mirror as PASSes.
                for directory, dirs, files in os.walk(api):
                    os.chown(directory,1000,1000)
                    for filename in files:
                        path = Path(directory)/filename
                        if not path.is_socket():
                            os.chown(path,1000,1000)
        with phase('pixel-parity'):
            run('prefetch-pixel-parity',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/pixel_conversion_parity.py','--root',str(r),
                '--base-command',str(r/'artifacts/baseline-command.json'),
                '--run-id',a.run_id],timeout=21000)
            pixels = json.loads((r/'artifacts/pixel-parity-result.json').read_text())
            assert pixels['passed'] and all(v['image_id'] == a.image for v in pixels['runs'])
        with phase('independent-marked-latency'):
            marked = native('marked-latency-SA-1x8',['--profile','SA','--marker-replay',
                   '--recording',str(r/'fixtures/marked-anpr720p10.mp4')],
                   gallery_limit=3000, gallery_interval_seconds=0)
            run('marked-latency-analysis',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/analyze_markers.py',str(marked/'gallery'),
                str(marked/'sender-epochs')],timeout=120)
            latency = json.loads((r/'artifacts/post-marked-latency-analysis/launch.log').read_text())
            measured = latency.get('steady_per_camera_ms', {})
            assert measured and all(v['samples'] >= 10 and v['min'] >= 0 and v['p95'] <= 4000
                                    for v in measured.values()), 'Missing markers or scene freshness exceeds declared four-second test bound'
            save(r/'artifacts/independent-marked-latency.json', latency)
        with phase('independent-marked-people-fleet-latency'):
            frontier=json.loads((r/'artifacts/continuation-frontier.json').read_text())
            count=frontier.get('SA-8',{}).get('last_short_passing_count',0)
            assert 1<=count<=50,'No passing all-app8FPS point for independent load latency'
            marked=native('marked-people-latency-SA-'+str(count)+'x8',
                ['--profile','SA','--marker-replay',
                 '--recording',str(r/'fixtures/marked-people1080p30.mp4')],
                cameras=count,fps=8,gallery_limit=3000,gallery_interval_seconds=0)
            run('marked-people-latency-analysis',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/analyze_markers.py',str(marked/'gallery'),
                str(marked/'sender-epochs')],timeout=120)
            latency=json.loads((r/'artifacts/post-marked-people-latency-analysis/launch.log').read_text())
            measured=latency.get('steady_per_camera_ms',{})
            assert len(measured)==count and all(v['samples']>=10 and v['min']>=0 and v['p95']<=4000
                for v in measured.values()),'Missing fleet pixel markers or scene age exceeds four-second test bound'
            save(r/'artifacts/independent-marked-people-fleet-latency.json',latency)
        with phase('borderline-short-frontier-repeats'):
            # Repeat only adjacent failures close to the declared window bound.
            # Preserve the original FAIL and do not replace the frontier with a
            # later favorable sample. This measures run-to-run uncertainty.
            frontier=json.loads((r/'artifacts/continuation-frontier.json').read_text())
            rows=json.loads((r/'artifacts/continuation-ledger.json').read_text())
            repeated=[]
            for key, boundary in sorted(frontier.items()):
                count=boundary.get('first_short_failing_count')
                if not count:continue
                profile, fps_text=key.split('-')
                fps=int(fps_text)
                original='turbo-off-next-'+profile+'-'+str(count)+'x'+str(fps)
                matches=[row for row in rows if row.get('case')==original]
                if len(matches)!=1:continue
                completed=matches[0].get('steady_completed_work',{}).get('inferred_work',{}).get('per_camera',{})
                if not completed or min(v.get('ratio',0) for v in completed.values())<.95:continue
                worst=min(v.get('minimum_window_ratio',0) for v in completed.values())
                if not .88<=worst<.90:continue
                result=native('borderline-repeat-'+profile+'-'+str(count)+'x'+str(fps),
                    ['--profile',profile,'--source-urls-json',json.dumps(
                     json.loads((r/'people50-source-urls.json').read_text())[:count])],
                    cameras=count,fps=fps,allow_capacity_failure=True)
                report=json.loads((result/'report.json').read_text())
                repeated.append({'original_case':original,'original_status':'FAIL',
                    'original_minimum_window_ratio':worst,'repeat_passed':report['passed'],
                    'repeat_report':str(result/'report.json'),
                    'scope':'Independent same-image repeat, original frontier not overwritten'})
                save(r/'artifacts/borderline-repeat-results.json',repeated)
        with phase('native-motion-quiet-wake-quiet'):
            expected={'capacity-01':{'embedding_profiles':{name:1 for name in
                ['adaface-ir101-v18.1','transreid-ssl-v18.1','gaitbase-v18.1','siglip2-base-v1']}}}
            motion=native('motion-quiet-people-quiet-SA-1x8',
                ['--profile','SA','--recording',str(r/'fixtures/motion-black-people1080p30.mp4'),
                 '--activity','mixed','--sample-seconds','5',
                 '--expected-outputs-json',json.dumps(expected)],steady_seconds=150,
                gallery_limit=1000,gallery_interval_seconds=0)
            run('motion-transition-analysis',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/analyze_motion_transitions.py',str(motion),
                '--output',str(r/'artifacts/motion-transition-result.json')],timeout=120)
        with phase('http-input'):
            server_output = r/'artifacts/post-http-source'
            with (r/'artifacts/post-http-server.log').open('w') as log:
                server = subprocess.Popen(['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/http_replay_source.py',
                    '--fixture',str(r/'fixtures/people1080p30.mp4'),'--output',str(server_output),
                    '--port','18880','--seconds','7200'],cwd=r/'repo',env=env,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try:
                    time.sleep(2)
                    assert server.poll() is None, 'HTTP fixture did not start'
                    native('http-S-1x8',['--profile','S','--source-url','http://127.0.0.1:18880/live.ts'])
                finally:
                    server.terminate()
                    server.wait(timeout=20)
        with phase('https-input-explicit-trusted-fixture'):
            from scripts.isolated_validation.https_fixture import trusted_https_fixture
            output = r/'artifacts/post-https-source'
            try:
                with trusted_https_fixture(r, output) as (url, trust_hook):
                    native('https-SA-1x8', ['--profile','SA','--source-url',url,
                           '--diagnostic-hook',str(trust_hook)])
            finally:
                if output.exists():
                    for directory, dirs, files in os.walk(output):
                        os.chown(directory,1000,1000)
                        for filename in files:
                            path=Path(directory)/filename
                            if not path.is_socket():os.chown(path,1000,1000)
        with phase('plain-rtsp-input'):
            native('plain-rtsp-SA-1x8', ['--profile','SA','--source-url',
                   'rtsp://192.168.1.92:18555/recovery'])
        # This fixed recording fleet separates image effects from changing live
        # traffic. A capacity failure is recorded, never promoted to a pass.
        with phase('frozen-six-diverse-inputs'):
            urls = ['rtsps://192.168.1.92:18557/'+name for name in
                    ['camera01','ch9','ch10','ch16','traffic1','traffic2']]
            native('frozen-six-SA-6x5', ['--profile','SA','--source-urls-json',
                   json.dumps(urls),'--activity','mixed'], cameras=6, fps=5,
                   steady_seconds=180, allow_capacity_failure=True)
        with phase('shared-tcp-consumers'):
            source_output = r/'artifacts/post-tcp-multiclient-source'
            with (r/'artifacts/post-tcp-source.log').open('w') as log:
                source = subprocess.Popen(['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/tcp_replay_source.py',
                    '--fixture',str(r/'fixtures/people1080p30.mp4'),'--output',str(source_output),
                    '--port','18881','--seconds','7200'],cwd=r/'repo',env=env,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try:
                    deadline = time.monotonic()+10
                    while not (source_output/'ready.json').exists() and time.monotonic()<deadline:
                        assert source.poll() is None, 'TCP fixture exited'
                        time.sleep(.1)
                    assert (source_output/'ready.json').exists(), 'TCP fixture not ready'
                    native('shared-tcp-S-5x5',['--profile','S','--source-urls-json',
                            json.dumps(['tcp://127.0.0.1:18881']*5)],cameras=5,fps=5)
                finally:
                    source.terminate()
                    source.wait(timeout=20)
                stats=json.loads((source_output/'finished.json').read_text())
                assert len([c for c in stats['connections'] if c['bytes_submitted']>0]) >= 5
        with phase('native-stage-timing'):
            traced = native('native-stage-timing-SA-1x8',['--profile','SA',
                '--recording',str(r/'fixtures/people1080p30.mp4'),
                '--diagnostic-hook',str(r/'repo/scripts/isolated_validation/gst_timing_diagnostic_hook.py'),
                '--container-env','GST_TRACERS=latency(flags=pipeline+element+reported)',
                '--container-env','GST_DEBUG=GST_TRACER:7',
                '--container-env','GST_DEBUG_NO_COLOR=1',
                '--container-env','GST_DEBUG_FILE=/run/sentinel-delivery/gst-latency.log'],
                steady_seconds=60)
            run('native-latency-analysis',['taskset','-c','14,15','python3',
                'scripts/isolated_validation/analyze_gst_latency.py',str(traced),
                '--output',str(r/'artifacts/native-element-latency.json')],timeout=120)
        # Same image/policy, explicit per-camera roles, scene enabled everywhere.
        # Report observed boundaries only; neither thresholds nor FPS acceptance
        # are relaxed when a role combination exceeds the available execution.
        people_sources = json.loads((r/'people50-source-urls.json').read_text())[:8]
        assert len(people_sources) == 8
        role_results = []
        for fps in [5, 8, 10]:
            with phase('identity-role-budget-'+str(fps)+'fps'):
                low, high = 0, None
                pending = [4, 8] if fps == 5 else [2, 4, 8]
                while pending:
                    identity_count = pending.pop(0)
                    profiles = ['SIFBG']*identity_count + ['S']*(8-identity_count)
                    expected = {f'capacity-{i+1:02d}': {'embedding_profiles': {
                        'adaface-ir101-v18.1': 1, 'transreid-ssl-v18.1': 1,
                        'gaitbase-v18.1': 1}} for i in range(identity_count)}
                    result = native(f'role-budget-eight-{identity_count}identity-{fps}fps',[
                        '--camera-profiles-json',json.dumps(profiles),
                        '--source-urls-json',json.dumps(people_sources),
                        '--expected-outputs-json',json.dumps(expected)],cameras=8,fps=fps,
                        allow_capacity_failure=True)
                    report = json.loads((result/'report.json').read_text())
                    role_results.append({'fps': fps, 'camera_count': 8,
                        'identity_cameras': identity_count, 'profiles': profiles,
                        'passed': report['passed'], 'report_path': str(result/'report.json')})
                    save(r/'artifacts/mixed-role-results.json', role_results)
                    if report['passed']:
                        low = max(low, identity_count)
                    else:
                        high = identity_count if high is None else min(high, identity_count)
                        pending = []
                    if not pending and high is not None and high-low > 1:
                        pending = [(low+high)//2]
        vehicle_file = r/'mixed-vehicle-source-urls.json'
        assert vehicle_file.is_file(), 'Mixed-resolution source manifest missing'
        vehicles = json.loads(vehicle_file.read_text())
        assert len(vehicles) >= 4
        for count in [2, 8]:
            with phase('mixed-roles-'+str(count)+'x8'):
                half = count//2
                profiles = ['SIFBG']*half + ['SVX']*half
                sources = people_sources[:half] + vehicles[:half]
                expected = {f'capacity-{i+1:02d}': {'embedding_profiles': {
                    'adaface-ir101-v18.1': 1, 'transreid-ssl-v18.1': 1,
                    'gaitbase-v18.1': 1}} for i in range(half)}
                expected.update({f'capacity-{i+1:02d}': {'events': {'plate_read': 1}}
                                 for i in range(half, count)})
                result = native(f'mixed-people1080-vehicle720-{count}x8',[
                    '--camera-profiles-json',json.dumps(profiles),
                    '--source-urls-json',json.dumps(sources),
                    '--expected-outputs-json',json.dumps(expected)],cameras=count,fps=8,
                    allow_capacity_failure=True)
                report = json.loads((result/'report.json').read_text())
                role_results.append({'fps': 8, 'camera_count': count, 'profiles': profiles,
                    'passed': report['passed'], 'report_path': str(result/'report.json')})
                save(r/'artifacts/mixed-role-results.json', role_results)
        # Live source availability is independently recorded, not inferred from
        # historical URLs. Failed live checks remain separate from replay capacity.
        source_manifest = r/'artifacts/user-source-availability-20261112.json'
        if source_manifest.exists():
            from urllib.parse import urlsplit
            available = [row for row in json.loads(source_manifest.read_text())['sources']
                         if row.get('exit_code') == 0 and row.get('streams')
                         and urlsplit(row['source']).scheme == 'rtsps']
            for index, source in enumerate(available, 1):
                with phase('live-rtsps-source-'+str(index)):
                    native('live-rtsps-'+str(index)+'-SA-1x8',
                           ['--profile','SA','--source-url',source['source'],
                            '--activity','mixed'], fps=8)
            if available:
                with phase('live-rtsps-mixed-fleet'):
                    native('live-rtsps-mixed-SA-'+str(len(available))+'x5',
                           ['--profile','SA','--source-urls-json',
                            json.dumps([row['source'] for row in available]),
                            '--activity','mixed'],
                           cameras=len(available), fps=5)
        with phase('kubernetes-projected-config-and-replacement'):
            # Only private image-copy preparation uses a2GiB disk reserve;
            # the helper restores/enforces6GiB before any CV pod is admitted.
            try:
                run('kubernetes-current',['taskset','-c','14,15','python3',
                    'scripts/isolated_validation/run_guarded_case.py',
                    '--root',str(r/'artifacts/kubernetes-guards'),'--run-id',a.run_id,
                    '--case','post-kubernetes-current','--engine','docker',
                    '--min-free-disk-mib','2048','--min-available-ram-mib','4096',
                    '--max-attempts','3','--max-runtime','2400','--max-cooldown','86400',
                    '--sample-interval','.5','--hot-samples','9','--stop-grace','90','--',
                    'taskset','-c','14,15','python3',
                    'scripts/isolated_validation/kubernetes_current_image.py',
                    '--root',str(r),'--image',a.image,'--run-id',a.run_id,
                    '--source-url',people_sources[0]],timeout=172800)
            finally:
                guarded=r/'artifacts/kubernetes-guards'
                if guarded.exists():
                    for path in [guarded,*guarded.rglob('*')]:
                        if not path.is_symlink():os.chown(path,1000,1000)
            proof=json.loads((r/'artifacts/kubernetes-current/report.json').read_text())
            assert proof['passed'] and proof['image_config_id']==a.image
        status('POSTCHECKS_FINISHED_REQUIRES_REVIEW',
               independent_phases=phases,
               note='Not all282 cases; all failed phases retained, no blanket production qualification or publication')
    except BaseException as exc:
        status('STOPPED' if stop[0] or (r/'STOP').exists() else 'NEEDS_REVIEW',
               reason=type(exc).__name__+': '+str(exc))
        raise


if __name__ == '__main__':
    main()
