"""Regression controls for the explicit native admission fault and its oracle."""
import copy
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'edge_runtime/solution_packs/sporada_secure/runtime_v8/services/worker'))
from pipeline import sentinel_v8_handoff as handoff
from scripts.isolated_validation import policy_admission_fault_hook as hook
from scripts.isolated_validation.analyze_policy_admission import analyze


@pytest.fixture
def scheduler(monkeypatch):
    class Scheduler:
        def __init__(self):
            self.condition = threading.Condition()
            self.counters = {'backpressure': 0}
            self.last_error = None
            self.forwarded = []

        def _snapshot_locked(self):
            pass

        def _admit(self, request):
            self.forwarded.append(request)
            return True, 'admitted'

    monkeypatch.setattr(handoff, 'V8HandoffScheduler', Scheduler)
    hook.install()
    return Scheduler()


def request(kind='plate_read', camera='capacity-01'):
    return {'camera_id': camera, 'manifest': {
        'package_id': 'first-package', 'observation': {
            'camera_id': camera, 'event_type': kind, 'observation_id': 'observation-1',
            'presence_id': 'presence-1', 'source_frame_id': camera + ':1',
            'observed_at': '2026-11-12T00:00:00Z',
        }, 'evidence': [{'metadata': {'evidence_id': 'image-1', 'sha256': 'hash-1',
            'size_bytes': 5, 'source_frame_id': camera + ':1', 'captured_at': '2026-11-12T00:00:00Z'}}],
    }, 'evidence_parts': [('evidence_0', b'frame')]}


@pytest.mark.parametrize('kind', ['plate_read', 'plate_outcome'])
def test_hook_refuses_once_then_observes_identical_logical_retry(scheduler, caplog, kind):
    value = request(kind)
    before = copy.deepcopy(value)
    with caplog.at_level('INFO'):
        assert scheduler._admit(value) == (False, 'delivery_backpressure')
        assert not scheduler.forwarded
        assert value == before
        retry = copy.deepcopy(value)
        retry['manifest']['package_id'] = 'second-local-admission-package'
        assert scheduler._admit(retry) == (True, 'admitted')
    assert scheduler.counters['backpressure'] == 1
    observed = [v for v in caplog.records if getattr(v, 'event', None) == 'validation_policy_admission_retry']
    assert len(observed) == 1 and observed[0].matches_initial_logical_payload is True
    assert retry['manifest']['observation'] == before['manifest']['observation']


@pytest.mark.parametrize('camera,kind', [('capacity-02', 'plate_read'), ('capacity-01', 'scene_sample')])
def test_peer_and_unrelated_work_are_not_refused(scheduler, camera, kind):
    value = request(kind, camera)
    assert scheduler._admit(value) == (True, 'admitted')
    assert scheduler.counters['backpressure'] == 0


def test_changed_retry_is_reported_without_modifying_accepted_work(scheduler, caplog):
    value = request()
    with caplog.at_level('INFO'):
        scheduler._admit(value)
        changed = copy.deepcopy(value)
        changed['manifest']['observation']['observation_id'] = 'different-observation'
        assert scheduler._admit(changed) == (True, 'admitted')
    observed = [v for v in caplog.records if getattr(v, 'event', None) == 'validation_policy_admission_retry']
    assert len(observed) == 1 and observed[0].matches_initial_logical_payload is False
    assert scheduler.forwarded == [changed]


def fixture_data(root):
    report = {'image_config_id': 'exact-image', 'diagnostic_hook_injected': True,
              'passed': True, 'fatal_lines': [], 'source_utc_sanity': {'passed': True}}
    events, packages = [], []
    for kind in ['plate_read', 'plate_outcome']:
        evidence = {'evidence_id': kind + '-image', 'sha256': 'hash', 'size_bytes': 5,
                    'source_frame_id': 'capacity-01:1', 'captured_at': '2026-11-12T00:00:00Z'}
        first = {'event': 'validation_policy_admission_refused', 'event_type': kind,
                 'camera_id': 'capacity-01', 'observation_id': kind + '-observation',
                 'presence_id': 'presence-1', 'source_frame_id': 'capacity-01:1',
                 'observed_at': '2026-11-12T00:00:00Z', 'logical_payload_digest': kind + '-digest',
                 'evidence_fingerprints': [evidence]}
        events.append(first)
        events.append(dict(first, event='validation_policy_admission_retry',
                           matches_initial_logical_payload=True))
        packages.append({**{k: first[k] for k in ['camera_id', 'event_type', 'observation_id',
                         'presence_id', 'source_frame_id', 'observed_at']},
                         'evidence': ['frame'], 'evidence_metadata': [evidence]})
    return report, events, packages


@pytest.mark.parametrize('failure', [None, 'missing-refusal', 'wrong-observation', 'wrong-image',
                                    'wrong-bytes', 'wrong-time', 'no-hook', 'missing-agent'])
def test_oracle_requires_exact_refused_observation_and_evidence(tmp_path, failure):
    report, events, packages = fixture_data(tmp_path)
    if failure == 'missing-refusal':
        events = [v for v in events if v['event_type'] != 'plate_outcome']
    elif failure == 'wrong-observation':
        packages[0]['observation_id'] = 'different-observation'
    elif failure == 'wrong-image':
        report['image_config_id'] = 'other-image'
    elif failure == 'wrong-bytes':
        packages = copy.deepcopy(packages)
        packages[0]['evidence_metadata'][0]['sha256'] = 'different-bytes'
    elif failure == 'wrong-time':
        packages[0]['observed_at'] = '2026-11-12T00:00:02Z'
    elif failure == 'no-hook':
        report['diagnostic_hook_injected'] = False
    elif failure == 'missing-agent':
        packages = packages[1:]
    (tmp_path / 'report.json').write_text(json.dumps(report))
    (tmp_path / 'container.log').write_text('\n'.join(map(json.dumps, events)))
    (tmp_path / 'packages.jsonl').write_text('\n'.join(map(json.dumps, packages)))
    assert analyze(tmp_path, 'exact-image')['passed'] is (failure is None)
