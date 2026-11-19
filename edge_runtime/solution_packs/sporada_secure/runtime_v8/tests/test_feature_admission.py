"""Producer sent-quality must advance only after local handoff admission."""
from pathlib import Path
from types import SimpleNamespace
import sys
import uuid
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'services/worker'))
from pipeline.v8_events import V8EventPipeline


class Store:
    def __init__(self):
        self.allow = False
        self.attempts = []
        self.accepted = []

    def persist(self, observation, evidence, embeddings):
        package = (observation, evidence, embeddings)
        self.attempts.append(package)
        if self.allow:
            self.accepted.append(package)
        return self.allow

    def close(self):
        pass


@pytest.mark.parametrize('kind,profile,dimension', [
    ('face','adaface-ir101-v18.1',512),
    ('body','transreid-ssl-v18.1',384), ('gait','gaitbase-v18.1',4096)])
def test_rejected_first_sample_can_be_sent_later_without_false_improvement(kind, profile, dimension):
    store = Store()
    binding = dict(use_case_id='identity', app='person_identity', scope='full_frame',
                   geometry_ids=[], object_types=['person'], embedding_profile_ids=[profile])
    camera = {'analytics':{'v8_events':{'v8':{
        'deployment_id':'test', 'config_revision':1, 'bindings':[binding],
        'geometry':{'zones':[], 'counting_lines':[]}}}}}
    pipeline = V8EventPipeline('cam', camera, handoff_client=store)
    state = dict(id=str(uuid.uuid4()), track_id=1, last_feature={})
    pipeline.presences[state['id']] = state
    packet = SimpleNamespace(index=1, frame=np.full((200,100,3),128,np.uint8))
    vector = np.zeros(dimension,np.float32);vector[0] = 1
    sequence = ({'sequence_started_at':'1970-01-01T00:01:10Z',
                 'sequence_ended_at':'1970-01-01T00:01:40Z',
                 'usable_silhouette_count':30} if kind == 'gait' else None)
    candidates = [(kind,vector,.5,(10,10,90,190),profile,sequence)]
    pipeline._emit_feature_candidates(packet,state,binding,candidates,100,'cam:1')
    if kind == 'face':pipeline._flush_face_candidates(102)
    assert store.attempts and not store.accepted
    assert state['last_feature'] == {}
    original = store.attempts[-1]
    if kind == 'face':
        pipeline._flush_face_candidates(102.1)
        assert len(store.attempts) == 1  # Bounded retry, no per-frame JPEG churn.
    store.allow = True
    if kind == 'face':
        # Collection has backed off: no new face/model/candidate is offered.
        pipeline._flush_face_candidates(106)
    else:
        pipeline._emit_feature_candidates(packet,state,binding,candidates,106,'cam:2')
    assert len(store.accepted) == 1
    assert state['last_feature'][kind] == (.5,100 if kind == 'face' else 106)
    observation, evidence, embeddings = store.accepted[0]
    assert observation['sample_reason'] == 'first_qualified'
    assert evidence[0][0]['captured_at'] == observation['observed_at']
    assert embeddings[0]['presence_id'] == state['id']
    assert embeddings[0]['source_frame_id'] == ('cam:1' if kind == 'face' else 'cam:2')
    if kind == 'face':
        assert observation['observation_id'] == original[0]['observation_id']
        assert evidence[0][1] == original[1][0][1]
        assert embeddings == original[2]
    # Normal accepted-sample dedup is unchanged, even after its cooldown.
    attempts = len(store.attempts)
    pipeline._emit_feature_candidates(packet,state,binding,candidates,120,'cam:3')
    if kind == 'face':pipeline._flush_face_candidates(122)
    assert len(store.attempts) == attempts
    pipeline.close()


@pytest.mark.parametrize('boundary', ['final','orphan'])
def test_rejected_face_does_not_leak_past_final_boundary_or_presence(boundary):
    from unittest.mock import Mock
    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    candidate = dict(deadline=100, retry_after=101, captured_at=99, quality=.5,
                     pending_package=({'observation_id':'original'}, ('metadata',b'jpeg'), {'embedding_id':'original'}))
    pipeline.face_candidates = {'p': candidate}
    pipeline.presences = {'p': {'last_feature':{}}} if boundary == 'final' else {}
    pipeline.telemetry = Mock()
    pipeline._persist = Mock(return_value=False)
    pipeline._flush_face_candidates(100.5 if boundary == 'final' else 102,
                                    final=boundary == 'final')
    assert not pipeline.face_candidates
    pipeline._persist.assert_called_once()
    pipeline.telemetry.count.assert_called_once_with('face_unadmitted_candidate_discarded')
