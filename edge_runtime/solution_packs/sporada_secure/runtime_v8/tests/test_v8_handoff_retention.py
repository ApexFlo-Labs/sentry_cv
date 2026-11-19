"""Cancelled packages must release actual payloads, not just counters.

Workers are deliberately not started: the scheduler must remain bounded while
all delivery workers are waiting on the agent. No model or network is involved.
"""
import hashlib
import uuid

from pipeline.sentinel_v8_handoff import (
    MAX_RETAINED_PACKAGES, PRIORITY_CRITICAL, PRIORITY_DURABLE,
    PRIORITY_IDENTITY, PRIORITY_REPLACEABLE, V8HandoffScheduler,
)


def request(index, *, key=None, priority=PRIORITY_REPLACEABLE):
    body = bytes([index % 256]) * 4096
    return {
        'manifest': {'package_id': str(uuid.UUID(int=index+1))},
        'evidence_parts': [('evidence_0', body)],
        'digest': hashlib.sha256(body).hexdigest(),
        'size_bytes': len(body)+256,
        'priority': priority, 'replaceable': priority == PRIORITY_REPLACEABLE,
        'semantic_key': key, 'observed_timestamp': float(index),
        'camera_id': 'test-camera',
    }


def scheduler(tmp_path):
    return V8HandoffScheduler({}, '/unused-test-agent', tmp_path/'status.json')


def test_replacement_flood_releases_old_payloads_and_keeps_latest(tmp_path):
    s = scheduler(tmp_path)
    key = ('scene', 'camera', 'binding')
    last_cancelled = None
    for i in range(256):
        old = s.pending_by_key.get(key)
        req = request(i, key=key)
        assert s._admit(req)[0]
        if old is not None:
            assert old.released and old.cancelled
            assert old.request == {}
            last_cancelled = old
        assert len(s.heap) == len(s.pending_by_key) == s.retained_count == 1
        assert s.retained_bytes == req['size_bytes']
    assert s.counters['coalesced'] == 255
    active = s.pending_by_key[key]
    s._release(last_cancelled)  # An idempotent cleanup cannot remove its successor.
    assert s.pending_by_key[key] is active
    assert s._take() is active
    assert active.request is req
    assert active.request['evidence_parts'][0][1] == bytes([255])*4096


def test_eviction_removes_semantic_index_and_keeps_priority_order(tmp_path):
    s = scheduler(tmp_path)
    optional = [request(i, key=('scene', i)) for i in range(4)]
    durable = [request(i, priority=PRIORITY_DURABLE) for i in range(4, 8)]
    for req in optional+durable:
        assert s._admit(req)[0]
    victim = s.pending_by_key[('scene', 3)]
    critical = request(8, priority=PRIORITY_CRITICAL)
    assert s._admit(critical)[0]
    assert victim.released and victim.cancelled and victim.request == {}
    assert ('scene', 3) not in s.pending_by_key
    assert len(s.heap) == s.retained_count == MAX_RETAINED_PACKAGES
    assert all(not item[3].cancelled for item in s.heap)
    expected = [critical, *durable, *optional[:3]]
    for original in expected:
        retained = s._take()
        assert retained.request is original
        assert hashlib.sha256(retained.request['evidence_parts'][0][1]).hexdigest() == original['digest']
        s._release(retained)
        assert retained.request == {}
        # Releasing queue ownership must not mutate caller-owned request bytes.
        assert original['evidence_parts'][0][1]
    assert not s.pending_by_key and not s.heap
    assert s.retained_count == s.retained_bytes == s.replaceable_count == s.replaceable_bytes == 0


def test_inflight_package_is_not_cancelled_or_changed_by_eviction(tmp_path):
    s = scheduler(tmp_path)
    key = ('scene', 'camera', 'binding')
    original = request(0, key=key)
    assert s._admit(original)[0]
    inflight = s._take()
    replacement = request(1, key=key)
    assert s._admit(replacement)[0]
    with s.condition:
        assert s._evict_replaceable()
        assert not s._evict_replaceable()
    assert inflight.submitted and not inflight.cancelled and not inflight.released
    assert inflight.request is original
    assert inflight.request['evidence_parts'][0][1] == bytes(4096)
    assert s.retained_count == 1 and s.retained_bytes == original['size_bytes']
    s._release(inflight)  # Simulate completion only after the assertions above.
    assert inflight.request == {} and original['evidence_parts'][0][1] == bytes(4096)


def test_finishing_old_inflight_request_cannot_remove_new_pending_key(tmp_path):
    s = scheduler(tmp_path)
    key = ('count', 'camera', 'binding')
    assert s._admit(request(0, key=key))[0]
    inflight = s._take()
    assert s._admit(request(1, key=key))[0]
    successor = s.pending_by_key[key]
    s._release(inflight)
    assert s.pending_by_key[key] is successor
    assert s.retained_count == 1


def test_repeated_different_scope_evictions_leave_no_payload_index(tmp_path):
    s = scheduler(tmp_path)
    for index in range(256):
        assert s._admit(request(index, key=('presence', index)))[0]
        with s.condition:
            assert s._evict_replaceable()
        assert not s.pending_by_key and not s.heap
        assert s.retained_count == s.retained_bytes == 0


def test_identity_and_durable_work_are_never_eviction_candidates(tmp_path):
    s = scheduler(tmp_path)
    requests = [request(0, priority=PRIORITY_IDENTITY),
                request(1, priority=PRIORITY_DURABLE)]
    for req in requests:
        assert s._admit(req)[0]
    with s.condition:
        assert not s._evict_replaceable()
    assert s._take().request is requests[1]
    assert s._take().request is requests[0]
