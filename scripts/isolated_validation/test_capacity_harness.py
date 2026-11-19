"""Offline measurement/fixture regressions; never launches a camera or model."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import os

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('guarded_capacity_probe', ROOT / 'scripts/v8_capacity_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize('engine', ['docker', 'podman'])
def test_explicit_startup_quota_restored_without_conflicting_nanocpus(engine):
    assert probe.startup_quota_release_command(engine, 'owned', 16, 10000) == [
        engine, 'update', '--cpu-period', '10000', '--cpu-quota', '160000', 'owned']


def test_nanocpus_startup_uses_matching_restore_representation():
    assert probe.startup_quota_release_command('docker', 'owned', 16, None) == [
        'docker', 'update', '--cpus', '16', 'owned']


def test_fifty_distinct_source_urls_are_not_reported_as_default_url_reuse():
    urls=[f'tcp://192.0.2.1:{19001+i}'for i in range(50)]
    assert not probe.source_urls_reused(urls)
    assert probe.source_urls_reused([urls[0]]*5)
    assert probe.source_urls_reused([urls[i%10]for i in range(16)])


def test_scheduled_outage_does_not_require_images_when_source_is_offline():
    assert probe.available_duration(0,120,[(20,38)]) == 102
    assert probe.available_duration(0,120,[(20,38),(30,40)]) == 100
    assert probe.available_duration(0,120,[(-10,10),(110,140)]) == 100
    with pytest.raises(ValueError):
        probe.available_duration(0,120,[(38,20)])


def test_scene_outage_allowance_applies_only_to_interrupted_camera():
    state = probe.desired_state(2,5,full_usecases=True)
    probe.apply_profiles(state,['S','S'])
    packages=[]
    for camera in state['cameras']:
        for i in range(45):
            packages.append({'camera_id':camera['camera_id'],'accepted_mono':i*2.5+1,
                'event_type':'scene_sample','use_case_id':camera['bindings'][0]['use_case_id'],
                'evidence':['frame'],'embeddings':['siglip2-base-v1'],
                'evidence_metadata':[{}],'embedding_metadata':[{}]})
    result=probe.steady_package_coverage(packages,state,0,120,activity='active',
        unavailable_intervals={'capacity-01':[(20,38)]})
    assert result['per_camera']['capacity-01']['passed']
    assert not result['per_camera']['capacity-02']['passed']


@pytest.mark.parametrize('fault',['none','frozen','inference_frozen','stale','restart','not_ready','missing'])
def test_isolation_requires_every_peer_to_complete_work_throughout_outage(fault):
    rows=[]
    for index in range(3):
        peers={key:{'state':'healthy','last_frame_age_seconds':.2,'camera_process_pid':51,
                     'decoded_frames':index*50,'completed_frames':index*50,'reconnects':0,'decoder_errors':0}
               for key in ['capacity-02','capacity-03']}
        for value in peers.values():
            value.update(counter_uptime_seconds=index*10.,telemetry_fresh=True)
        rows.append({'ready':True,'sample_monotonic_seconds':index*10.,'cameras':peers})
    bad=rows[1]['cameras']['capacity-03']
    if fault=='frozen':bad['decoded_frames']=0
    if fault=='inference_frozen':bad['completed_frames']=0
    if fault=='stale':bad['last_frame_age_seconds']=8
    if fault=='restart':bad['camera_process_pid']=99
    if fault=='not_ready':rows[1]['ready']=False
    if fault=='missing':del rows[1]['cameras']['capacity-03']
    assert probe.isolation_peers_progress(rows,'capacity-01',
        {'capacity-01':5,'capacity-02':5,'capacity-03':5}) == (fault=='none')


def test_isolation_rate_uses_counter_clock_not_cached_http_poll_time():
    rows=[]
    for wall,uptime,selected,completed in [
        (30475.5774,57.086,190,177),(30485.5840,65.250,230,218),
        (30495.5910,75.433,281,269)]:
        rows.append({'sample_monotonic_seconds':wall,'ready':True,'cameras':{
            'capacity-02':{'state':'healthy','last_frame_age_seconds':1.,
                'camera_process_pid':52,'decoded_frames':selected,
                'completed_frames':completed,'reconnects':0,'decoder_errors':0,
                'counter_uptime_seconds':uptime,'telemetry_fresh':True}}})
    assert probe.isolation_peers_progress(rows,'capacity-01',{'capacity-01':5,'capacity-02':5})


@pytest.mark.parametrize('fault', ['none','reconnects','decoder_errors',
    'counter_reset','missing','negative','bool','float'])
def test_isolation_retains_startup_history_but_rejects_new_peer_errors(fault):
    rows=[]
    for i in range(3):
        rows.append({'ready':True,'sample_monotonic_seconds':100.+i*10,
            'cameras':{'capacity-02':{'state':'healthy','last_frame_age_seconds':.2,
                'camera_process_pid':51,'decoded_frames':7+i*80,
                'completed_frames':7+i*80,'reconnects':1,'decoder_errors':1,
                'counter_uptime_seconds':24.+i*10,'telemetry_fresh':True}}})
    value=rows[1]['cameras']['capacity-02']
    if fault in ['reconnects','decoder_errors']:value[fault]+=1
    if fault=='counter_reset':value['reconnects']=0
    if fault=='missing':value.pop('reconnects')
    if fault=='negative':value['reconnects']=-1
    if fault=='bool':value['reconnects']=True
    if fault=='float':value['reconnects']=1.
    assert probe.isolation_peers_progress(rows,'capacity-01',
        {'capacity-01':8,'capacity-02':8}) == (fault=='none')


@pytest.mark.parametrize('fault',['missing_clock','clock_frozen','clock_reversed',
                                'clock_nan','clock_bool','stale','slow'])
def test_isolation_counter_clock_correction_does_not_admit_stalls(fault):
    rows=[]
    for i in range(2):
        rows.append({'sample_monotonic_seconds':100.+i*10,'ready':True,'cameras':{
            'capacity-02':{'state':'healthy','last_frame_age_seconds':.2,
                'camera_process_pid':52,'decoded_frames':i*50,'completed_frames':i*50,
                'reconnects':0,'decoder_errors':0,'counter_uptime_seconds':i*10.,
                'telemetry_fresh':True}}})
    value=rows[-1]['cameras']['capacity-02']
    if fault=='missing_clock':value.pop('counter_uptime_seconds')
    if fault=='clock_frozen':value['counter_uptime_seconds']=0.
    if fault=='clock_reversed':value['counter_uptime_seconds']=-1.
    if fault=='clock_nan':value['counter_uptime_seconds']=float('nan')
    if fault=='clock_bool':value['counter_uptime_seconds']=True
    if fault=='stale':value['telemetry_fresh']=False
    if fault=='slow':value['completed_frames']=35
    assert not probe.isolation_peers_progress(rows,'capacity-01',{'capacity-01':5,'capacity-02':5})


def test_compilation_cache_is_image_and_host_bound_and_hash_checked(tmp_path):
    seed, output = tmp_path/'seed', tmp_path/'output'
    seed.mkdir()
    output.mkdir()
    (seed/'npu').mkdir()
    (seed/'ov').mkdir()
    blob = seed/'npu'/'model-compiled-key'
    blob.write_bytes(b'controlled compiled fixture, not real model')
    image = 'a'*64
    manifest = {'image_config_id': image, 'host': os.uname().nodename,
                'kernel': os.uname().release,
                'host_boot_id_sha256': hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest(),
                'files': {'npu/model-compiled-key': {
                    'size_bytes': blob.stat().st_size,
                    'sha256': hashlib.sha256(blob.read_bytes()).hexdigest()}}}
    (seed/'CACHE_SEED_MANIFEST.json').write_text(json.dumps(manifest))
    args = probe.stage_compilation_cache(seed, image, output)
    assert '/tmp/sentinel/cache/ze_intel_npu_cache:rw' in args[1]
    assert (output/'compilation-cache/npu/model-compiled-key').read_bytes() == blob.read_bytes()
    assert 'NOT cold-start' in (output/'compilation-cache-start.json').read_text()
    wrong = tmp_path/'wrong'
    wrong.mkdir()
    with pytest.raises(ValueError, match='exact image'):
        probe.stage_compilation_cache(seed, 'b'*64, wrong)
    blob.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='frozen manifest'):
        probe.stage_compilation_cache(seed, image, wrong)


def sample(t, a, b):
    return {'sample_phase': 'steady', 'sample_monotonic_seconds': t, 'target_decode_fps': 20,
            'per_camera': {key: {'totals': {'decoded_frames': n}}
                           for key, n in [('a', a), ('b', b)]}}


def test_slow_window_not_hidden_by_recovery_burst():
    rows = [sample(0, 0, 0), sample(10, 0, 0), sample(20, 200, 200)]
    assert not probe.steady_throughput(rows, .9)['passed']


def test_midrun_counter_reset_not_hidden_by_later_rebound():
    rows = [sample(0, 100, 100), sample(10, 10, 10), sample(20, 400, 400)]
    assert not probe.steady_throughput(rows, .9)['passed']


@pytest.mark.parametrize('field', ['stream_session_id', 'camera_process_pid'])
def test_generation_change_invalidates_uninterrupted_hold(field):
    rows = [sample(0, 0, 0), sample(10, 100, 100)]
    for row in rows:
        for camera in row['per_camera'].values():
            camera[field] = 'original'
    rows[-1]['per_camera']['a'][field] = 'replacement'
    assert not probe.steady_throughput(rows, .9)['passed']


def test_missing_health_or_camera_midrun_cannot_pass():
    rows = [sample(0, 0, 0), sample(10, 100, 100), sample(20, 200, 200)]
    rows[1]['per_camera'] = {}
    assert not probe.steady_throughput(rows, .9)['passed']


def test_mixed_rates_use_individual_desired_state_targets():
    rows = [sample(0, 0, 0), sample(10, 50, 100)]
    for row in rows:
        row['target_decode_fps'] = 15
        row['per_camera']['a']['target_fps'] = 5
        row['per_camera']['b']['target_fps'] = 10
    result = probe.steady_throughput(rows, .95)
    assert result['passed']
    assert {k: c['ratio'] for k, c in result['per_camera'].items()} == {'a': 1, 'b': 1}


@pytest.mark.parametrize('age,expected', [(0, True), (1, True), (None, False), (-1, False), (60, False)])
def test_zero_age_is_fresh_but_unknown_future_and_stale_are_not(age, expected):
    camera = {'processing': {'totals': {'decoded_frames': 100}},
              'camera_health': {'state': 'healthy'}, 'decoder': {'last_frame_age_seconds': age}}
    assert probe.camera_ready(camera) is expected


def test_source_url_credentials_and_query_tokens_are_redacted():
    result = probe.sanitized_url('rtsps://person:private@example.com:443/cam?token=secret')
    assert result == 'rtsps://example.com:443/cam?REDACTED'
    assert all(secret not in result for secret in ['person', 'private', 'secret'])


PROFILES = json.loads((ROOT / 'CAPACITY_TEST_MATRIX.json').read_text())['uniform_profiles']


@pytest.mark.parametrize('profile', PROFILES, ids=lambda p: p['profile_id'])
def test_all_declared_profiles_have_every_app_scene_and_exact_identity_modalities(profile):
    state = probe.desired_state(2, 8, full_usecases=True)
    probe.apply_profiles(state, [profile['profile_id']] * 2)
    schema = json.loads((ROOT / 'newdetails/sentry-v8/desired-state.schema.json').read_text())
    Draft202012Validator(schema).validate(state)
    for camera in state['cameras']:
        actual = {b['app'] for b in camera['bindings']}
        assert actual == set(profile['required_apps'])
        scene = next(b for b in camera['bindings'] if b['app'] == 'scene_search')
        assert scene['embedding_profile_ids'] == ['siglip2-base-v1']
        assert scene['sample_interval_seconds'] == 2
        assert set(scene['scene_trigger_use_case_ids']).issubset({b['use_case_id'] for b in camera['bindings']})
        identity = next((b for b in camera['bindings'] if b['app'] == 'person_identity'), None)
        if identity:
            assert identity['embedding_profile_ids'] == profile['identity_embedding_profile_ids']


@pytest.mark.parametrize('profiles', [['unknown', 'S'], ['S']])
def test_invalid_or_incomplete_role_map_is_rejected(profiles):
    with pytest.raises(ValueError):
        probe.apply_profiles(probe.desired_state(2, 8, full_usecases=True), profiles)


def test_tracked_agent_does_not_depend_on_ignored_run_directory():
    assert probe.AGENT.is_file()
    assert 'run/v8-live' not in str(probe.AGENT)


def test_cleanup_kills_and_reaps_only_registered_children(monkeypatch):
    resources = probe.ProbeResources('docker')
    child = Mock()
    child.poll.return_value = None
    child.wait.side_effect = [subprocess.TimeoutExpired('test child', 5), None]
    resources.processes.append(child)
    engine = Mock()
    monkeypatch.setattr(probe.subprocess, 'run', engine)
    resources.close()
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    assert child.wait.call_count == 2
    engine.assert_not_called()  # No container ID owned; no prefix-wide stopping.


def test_cleanup_uses_owned_cidfile_after_engine_timeout(tmp_path, monkeypatch):
    resources = probe.ProbeResources('docker')
    resources.cidfile = tmp_path / 'container-id.txt'
    resources.cidfile.write_text('a' * 64)
    engine = Mock()
    monkeypatch.setattr(probe.subprocess, 'run', engine)
    resources.close()
    assert engine.call_count == 2
    for invocation in engine.call_args_list:
        assert invocation.args[0][-1] == 'a' * 64
        assert '--filter' not in invocation.args[0]


def test_outer_cleanup_runs_even_on_source_or_agent_setup_failure(monkeypatch):
    child = Mock()
    child.poll.return_value = None

    def failed_setup(args, resources):
        resources.processes.append(child)
        raise RuntimeError('fixture failed before CV startup')

    monkeypatch.setattr(probe, '_run_probe', failed_setup)
    with pytest.raises(RuntimeError):
        probe.run_probe(SimpleNamespace(engine='docker'))
    child.terminate.assert_called_once()
    child.wait.assert_called_once()


def scene_record(camera, t):
    return {'camera_id': camera, 'accepted_mono': t, 'event_type': 'scene_sample',
            'use_case_id': 'scene-' + str(int(camera.rsplit('-', 1)[1])),
            'evidence': ['frame'], 'embeddings': ['siglip2-base-v1'],
            'evidence_metadata': [{'evidence_id': 'validated-at-api'}],
            'embedding_metadata': [{'profile_id': 'siglip2-base-v1', 'dims': 768}]}


def scene_state():
    state = probe.desired_state(2, 8, full_usecases=True)
    probe.apply_profiles(state, ['S', 'S'])
    return state


def test_fleet_scene_totals_cannot_hide_one_camera_without_snapshots():
    records = [scene_record('capacity-01', t) for t in range(10, 31, 2)]
    report = probe.steady_package_coverage(records, scene_state(), 10, 30, activity='active')
    assert not report['passed']
    assert report['per_camera']['capacity-01']['passed']
    assert not report['per_camera']['capacity-02']['passed']


def test_every_camera_paired_scene_packages_in_steady_window_pass():
    records = [scene_record(c, t) for c in ['capacity-01', 'capacity-02'] for t in range(10, 31, 2)]
    assert probe.steady_package_coverage(records, scene_state(), 10, 30, activity='active')['passed']


def test_startup_packages_cannot_cover_missing_steady_outputs():
    records = [scene_record(c, t) for c in ['capacity-01', 'capacity-02'] for t in range(0, 10)]
    assert not probe.steady_package_coverage(records, scene_state(), 10, 30)['passed']


def test_scene_embedding_without_evidence_is_not_complete_coverage():
    records = [scene_record(c, t) for c in ['capacity-01', 'capacity-02'] for t in range(10, 31, 2)]
    for record in records:
        record['evidence_metadata'] = []
    assert not probe.steady_package_coverage(records, scene_state(), 10, 30)['passed']


def test_enabled_identity_without_required_positive_output_is_not_workload_coverage():
    records = [scene_record(c, t) for c in ['capacity-01', 'capacity-02'] for t in range(10, 31, 2)]
    oracle = {'capacity-01': {'embedding_profiles': ['gaitbase-v18.1']}}
    report = probe.steady_package_coverage(records, scene_state(), 10, 30, expected=oracle)
    assert not report['passed']
    assert 'gaitbase-v18.1' in report['per_camera']['capacity-01']['errors'][0]


def test_quiet_scene_cadence_does_not_qualify_active_scene_cadence():
    records = [scene_record(c, t) for c in ['capacity-01', 'capacity-02'] for t in range(10, 61, 5)]
    assert probe.steady_package_coverage(records, scene_state(), 10, 60, activity='quiet')['passed']
    assert not probe.steady_package_coverage(records, scene_state(), 10, 60, activity='active')['passed']


def test_selected_surfaces_are_not_completed_work():
    rows = [sample(0, 0, 0), sample(10, 100, 100)]
    for row in rows:
        for camera in row['per_camera'].values():
            camera['totals'].update(inference_frames=0, quiet_frames=0)
    assert probe.steady_throughput(rows, .95)['passed']
    assert not probe.steady_completed_work(rows, .95, .9, 'mixed')['passed']


def test_quiet_completion_is_valid_but_does_not_qualify_busy_inference():
    rows = [sample(0, 0, 0), sample(10, 100, 100)]
    for row in rows:
        for camera in row['per_camera'].values():
            camera['totals'].update(inference_frames=0, quiet_frames=camera['totals']['decoded_frames'])
    assert probe.steady_completed_work(rows, .95, .9, 'quiet')['passed']
    assert not probe.steady_completed_work(rows, .95, .9, 'active')['passed']


def test_missing_completion_telemetry_is_not_a_pass():
    assert not probe.steady_completed_work([sample(0, 0, 0), sample(10, 100, 100)],
                                          .95, .9, 'active')['passed']


def test_exported_sparse_zero_counter_is_not_missing_telemetry():
    rows = [sample(0, 0, 0), sample(10, 100, 100)]
    for row in rows:
        for camera in row['per_camera'].values():
            camera['totals']['inference_frames'] = camera['totals']['decoded_frames']
    # Actual RuntimeTelemetry output omits quiet_frames when its count is zero.
    assert probe.steady_completed_work(rows, .95, .9, 'active')['passed']


def test_short_rolling_snapshot_cannot_override_actual_ten_second_interval():
    rows = [sample(0, 0, 0), sample(10, 100, 100), sample(20, 200, 200)]
    for row in rows:
        row['decode_target_ratio'] = .88
    assert probe.steady_throughput(rows, .95, .9)['passed']


def test_startup_affinity_ranges_are_explicit_not_a_steady_quota():
    assert probe.parse_cpu_set('0-3,8,14-15') == {0, 1, 2, 3, 8, 14, 15}
    for invalid in ['4-2', '-1', '1-2-3', '']:
        with pytest.raises(ValueError):
            probe.parse_cpu_set(invalid)


def test_future_timestamp_in_any_artifact_cannot_pass_causal_clock_check():
    base = {'accepted_at': '2026-11-10T12:00:01Z', 'observed_at': '2026-11-10T12:00:00Z'}
    assert probe.source_utc_sanity([base])['passed']
    for kind in ['evidence_metadata', 'embedding_metadata']:
        row = dict(base, **{kind: [{'captured_at': '2026-11-10T12:00:03Z'}]})
        result = probe.source_utc_sanity([row])
        assert not result['passed'] and result['future_count'] == 1
    assert not probe.source_utc_sanity([dict(base, observed_at='2026-11-10T12:00:03Z')])['passed']


def test_missing_clock_data_is_unmeasured_not_success():
    assert not probe.source_utc_sanity([])['passed']
    assert not probe.source_utc_sanity([{'observed_at': '2026-11-10T12:00:00Z'}])['passed']


def test_declared_cooling_only_adjusts_startup_clock(tmp_path, monkeypatch):
    clock = tmp_path / 'pause.json'
    clock.write_text(json.dumps({'total_seconds': 25.0}))
    monkeypatch.setenv('SENTINEL_VALIDATION_PAUSE_CLOCK', str(clock))
    monkeypatch.setattr(probe.time, 'monotonic', lambda: 100.0)
    assert probe.startup_monotonic() == 75.0
    assert probe.time.monotonic() == 100.0  # Real latency/steady sampling clock is unchanged.


def test_phase_record_is_explicit_and_atomic(tmp_path, monkeypatch):
    target = tmp_path / 'phase.json'
    monkeypatch.setenv('SENTINEL_VALIDATION_PHASE_FILE', str(target))
    probe.validation_phase('steady')
    assert json.loads(target.read_text())['phase'] == 'steady'
    assert not target.with_suffix('.tmp').exists()


def test_poll_period_does_not_replace_counter_publication_period():
    rows = [sample(0, 0, 0), sample(5, 20, 20), sample(10, 50, 50)]
    for row, uptime in zip(rows, [0, 4, 10]):
        row['target_decode_fps'] = 10
        for camera in row['per_camera'].values():
            camera['target_fps'] = 5
            camera['counter_uptime_seconds'] = uptime
    result = probe.steady_throughput(rows, .95, .9)
    assert result['passed']
    assert result['per_camera']['a']['minimum_window_ratio'] == 1


def test_old_utc_snapshot_records_can_be_audited_without_changing_raw_counts():
    rows = [sample(0, 0, 0), sample(5, 20, 20), sample(10, 50, 50)]
    for row, seconds in zip(rows, ['00', '04', '10']):
        row['target_decode_fps'] = 10
        for camera in row['per_camera'].values():
            camera['target_fps'] = 5
            camera['telemetry_observed_at'] = f'2026-11-10T12:00:{seconds}Z'
    result = probe.steady_throughput(rows, .95, .9)
    assert result['passed']
    assert result['per_camera']['a']['clock'] == 'reporter_utc'


def test_output_oracle_rejects_unknown_profile_before_hardware():
    state = probe.desired_state(1, 8, full_usecases=True)
    with pytest.raises(ValueError, match='profile not enabled'):
        probe.validate_output_oracle(state, {'capacity-01': {
            'embedding_profiles': {'transreid-ssl-v1': 1}}})
    probe.validate_output_oracle(state, {'capacity-01': {
        'embedding_profiles': {'transreid-ssl-v18.1': 1}}})


@pytest.mark.parametrize('rules', [
    {'missing-camera': {}}, {'capacity-01': {'events': {'object_present': 0}}},
    {'capacity-01': {'events': {'object_present': True}}},
    {'capacity-01': {'eventz': ['object_present']}},
])
def test_output_oracle_rejects_invalid_requirements(rules):
    with pytest.raises(ValueError):
        probe.validate_output_oracle(probe.desired_state(1, 8, full_usecases=True), rules)


def test_positive_oracle_minimum_is_not_just_key_presence():
    state = {'cameras': [{'camera_id': 'a', 'bindings': []}]}
    rows = [{'camera_id': 'a', 'accepted_mono': 1, 'event_type': 'object_present'}]
    result = probe.steady_package_coverage(rows, state, 0, 2,
        expected={'a': {'events': {'object_present': 2}}})
    assert not result['passed']


def test_scene_count_allows_next_eligible_frame_but_not_arbitrary_slow_sampling():
    state = probe.desired_state(1, 8, full_usecases=True)
    probe.apply_profiles(state, ['S'])
    scene = dict(camera_id='capacity-01',event_type='scene_sample',use_case_id='scene-1',
                 evidence=['frame'], evidence_metadata=[{}],embedding_metadata=[{}],
                 embeddings=['siglip2-base-v1'])
    rows = [dict(scene, accepted_mono=.5+i*2.125)for i in range(42)]
    assert probe.steady_package_coverage(rows,state,0,90,activity='active')['passed']
    slow = [dict(scene,accepted_mono=.5+i*3)for i in range(29)]
    assert not probe.steady_package_coverage(slow,state,0,90,activity='active')['passed']


def test_explicit_opencl_diagnostic_freezes_bytes_and_preserves_image_loader(tmp_path):
    import hashlib
    raw=bytearray(64);raw[:6]=b'\x7fELF\x02\x01';raw[16:18]=(3).to_bytes(2,'little');raw[18:20]=(62).to_bytes(2,'little')
    source=tmp_path/'input.so';source.write_bytes(raw);out=tmp_path/'output';out.mkdir()
    args=probe.stage_opencl_diagnostic(source,{'Config':{'Env':['LD_LIBRARY_PATH=/baked/sdk:/baked/models']}},out)
    assert 'LD_LIBRARY_PATH=/diagnostic-opencl:/baked/sdk:/baked/models' in args
    assert str((out/'diagnostic-opencl').resolve())+':/diagnostic-opencl:ro' in args
    assert (out/'diagnostic-opencl/libOpenCL.so').read_bytes()==raw
    source.write_bytes(b'changed after frozen')
    assert (out/'diagnostic-opencl/libOpenCL.so.1').read_bytes()==raw
    record=json.loads((out/'diagnostic-opencl.json').read_text())
    assert record['sha256']==hashlib.sha256(raw).hexdigest()
    assert record['settings']['CLI_OpenCLFileName']=='/usr/lib/x86_64-linux-gnu/libOpenCL.so.1'
    assert not any(k in record['settings']for k in ['CLI_FinishAfterEnqueue','CLI_FlushAfterEnqueue','CLI_NullEnqueue'])


def test_invalid_diagnostic_library_fails_before_loader_override(tmp_path):
    source=tmp_path/'bad.so';source.write_bytes(b'not an amd64 dynamic library')
    with pytest.raises(ValueError,match='amd64 ELF'):probe.stage_opencl_diagnostic(source,{},tmp_path)
    assert not (tmp_path/'diagnostic-opencl').exists()
