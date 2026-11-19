"""INC-010: rejected local admission must not advance production sample state.

Model-free semantic checks against the imported implementation; these do not
qualify native inference, accuracy, or durable storage by the delivery agent.
"""
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import numpy as np
import pytest

runtime_roots = [Path('/opt/traffic-pilot')]
parents = Path(__file__).resolve().parents
runtime_roots.extend(p for p in parents if (p / 'services/worker/pipeline/v8_events.py').is_file())
runtime_roots.extend(p / 'edge_runtime/solution_packs/sporada_secure/runtime_v8'
                     for p in parents if (p / 'edge_runtime/solution_packs/sporada_secure/runtime_v8').is_dir())
for root in runtime_roots:
    if (root / 'services/worker/pipeline/v8_events.py').is_file():
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / 'services/worker'))
        break

from pipeline.v8_events import V8EventPipeline


class RejectThenAccept:
    def __init__(self):
        self.calls = []
        self.accept = False

    def persist(self, event, evidence, embeddings):
        self.calls.append((event, evidence, embeddings))
        return self.accept

    def metrics_summary(self):
        return {'outbox_bytes': 0}

    def close(self):
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('APEXFABRIC_STATE_ROOT', str(tmp_path))
    store = RejectThenAccept()
    pipeline = V8EventPipeline('cam-1', {'analytics': {'v8_events': {'v8': {
        'deployment_id': 'admission-regression', 'config_revision': 1,
        'bindings': [], 'geometry': {'zones': [], 'counting_lines': []},
    }}}}, handoff_client=store)
    state = {'id': str(uuid.uuid4()), 'track_id': 7,
             'plates': {}, 'plate_outcomes': set(), 'last_feature': {}}
    pipeline.presences[state['id']] = state
    packet = SimpleNamespace(frame=np.full((100, 100, 3), 128, np.uint8))
    yield pipeline, state, store, packet
    pipeline.close()


def test_plate_read_rejection_does_not_record_sent_plate(context):
    pipeline, state, store, packet = context
    binding = {'use_case_id': 'anpr', 'geometry_ids': []}
    candidate = {'frame_shape': packet.frame.shape, 'containing': set(),
                 'captured_at': 100.0, 'frame_id': 'cam-1:1',
                 'text': 'GARAGE12', 'confidence': .95,
                 'bbox': (20, 40, 60, 55), 'frame': packet.frame}
    pipeline._emit_plate_read(state, binding, candidate, None, 103.0)
    assert 'anpr' not in state['plates'], 'Rejected handoff was recorded as a sent plate'
    refused = store.calls[-1]
    store.accept = True
    pipeline._emit_plate_read(state, binding, candidate, None, 103.1)
    assert state['plates']['anpr'][0] == 'GARAGE12'
    assert store.calls[-1] == refused, 'Admission retry changed source metadata, IDs or evidence bytes'


def test_plate_outcome_rejection_does_not_suppress_later_outcome(context):
    pipeline, state, store, packet = context
    attempt = {'last_captured_at': 100.0, 'last_frame_id': 'cam-1:1',
               'last_frame': packet.frame, 'last_frame_shape': packet.frame.shape}
    pipeline._emit_plate_outcome(state, 'anpr', attempt)
    assert 'anpr' not in state['plate_outcomes'], 'Rejected outcome was marked delivered'
    refused = store.calls[-1]
    store.accept = True
    pipeline._emit_plate_outcome(state, 'anpr', attempt)
    assert 'anpr' in state['plate_outcomes']
    assert store.calls[-1] == refused


@pytest.mark.parametrize('entrypoint', ['idle', 'frame'])
def test_rejected_best_plate_window_survives_without_new_ocr(context, monkeypatch, entrypoint):
    pipeline, state, store, packet = context
    binding = {'app': 'anpr', 'use_case_id': 'anpr', 'geometry_ids': []}
    pipeline.bindings = [binding]
    candidate = {'frame_shape': packet.frame.shape, 'containing': set(),
                 'captured_at': 100.0, 'frame_id': 'cam-1:1',
                 'text': 'GARAGE12', 'confidence': .95,
                 'bbox': (20, 40, 60, 55), 'frame': packet.frame}
    attempt = {'started': 99.0, 'candidates': [candidate],
               'seen_ocr_observations': {'cam-1:1'}}
    state.update(kind='vehicle', lost=False, plate_attempts={'anpr': attempt})
    packet.detections = []
    # Selection itself has independent policy tests. This check starts with
    # a selected result and exercises both post-window admission callers.
    monkeypatch.setattr(pipeline, '_plate_candidate_ready', lambda *args: candidate)

    def evaluate(now):
        if entrypoint == 'idle':
            pipeline._plate_idle_timeouts(now)
        else:
            pipeline._plates(packet, now, 'cam-1:2')

    evaluate(103.0)
    assert not state['plates']
    assert attempt['candidates'] == [candidate]
    assert attempt['seen_ocr_observations'] == {'cam-1:1'}
    assert attempt['started'] == 99.0
    rejected = store.calls[-1]
    store.accept = True
    evaluate(103.1)
    assert store.calls[-1] == rejected
    assert state['plates']['anpr'][0] == 'GARAGE12'
    assert attempt['candidates'] == []
    assert attempt['seen_ocr_observations'] == set()
    assert attempt['started'] == 103.1


def test_body_rejection_leaves_equal_quality_sample_eligible(context):
    pipeline, state, store, packet = context
    vector = np.zeros(384, np.float32)
    vector[0] = 1
    candidates = [('body', vector, .8, (20, 10, 80, 95), 'transreid-ssl-v18.1', None)]
    pipeline._emit_feature_candidates(packet, state, {'use_case_id': 'identity'}, candidates, 100, 'cam-1:1')
    assert not state['last_feature']
    store.accept = True
    pipeline._emit_feature_candidates(packet, state, {'use_case_id': 'identity'}, candidates, 100.1, 'cam-1:2')
    assert state['last_feature']['body'][0] == .8


def test_face_rejection_retains_same_package_and_source_sample(context):
    pipeline, state, store, packet = context
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    pipeline._offer_face_candidate(packet, state, {'use_case_id': 'identity'}, vector,
                                   .8, (30, 20, 55, 50), 100, 'cam-1:1')
    deadline = pipeline.face_candidates[state['id']]['deadline']
    pipeline._flush_face_candidates(deadline)
    assert 'face' not in state['last_feature']
    assert state['id'] in pipeline.face_candidates
    first = store.calls[-1]
    store.accept = True
    pipeline._flush_face_candidates(deadline + .3)
    assert store.calls[-1] == first
    assert state['last_feature']['face'] == (.8, 100)
    assert state['id'] not in pipeline.face_candidates
