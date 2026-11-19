"""Normal and prefetched surfaces retain the timestamp/consumption boundary."""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from pipeline.surface_stream import _record_consumed_surface


def sample(pts):
    return SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=pts))


def test_prefetched_surface_is_not_reported_consumed_during_retention():
    trace = Mock()
    retained = sample(12345)
    trace.assert_not_called()
    _record_consumed_surface(retained, trace)
    assert trace.mock_calls == [call.mark('camera.consumed', 12345),
                                call.phase('frame_processing')]


@pytest.mark.parametrize('trace', [None, Mock()])
def test_missing_pts_is_rejected_before_clock_admission_or_progress(trace):
    if trace is not None:
        trace.reset_mock()
    with pytest.raises(RuntimeError, match='surface timestamp missing'):
        _record_consumed_surface(sample((1 << 64)-1), trace)
    if trace is not None:
        assert not trace.mock_calls


def test_end_of_stream_is_not_a_fresh_consumed_frame():
    trace = Mock()
    _record_consumed_surface(None, trace)
    assert not trace.mock_calls


@pytest.mark.parametrize('pts', [0, 1, 10**18])
def test_valid_pts_unchanged_when_tracing_disabled(pts):
    frame = sample(pts)
    _record_consumed_surface(frame)
    assert frame.get_buffer().pts == pts
