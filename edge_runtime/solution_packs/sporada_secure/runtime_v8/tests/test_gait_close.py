"""Regression: a gait-enabled camera must be able to tear down for recovery."""
from pathlib import Path
from types import SimpleNamespace
import sys
import weakref

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline.person_reid import OpenVINOGaitExtractor
from pipeline.dls_secondary import DlsGaitExtractor


def extractor(cls):
    value = cls.__new__(cls)
    value.backgrounds = {'camera': cv2.createBackgroundSubtractorMOG2(
        history=300, varThreshold=25, detectShadows=False)}
    value.backgrounds['camera'].apply(np.zeros((36, 64, 3), np.uint8))
    value._foreground_cache = {'camera': ('frame-key', np.ones((36, 64), np.uint8))}
    return value


def test_native_gait_background_teardown_is_available_and_idempotent():
    value = extractor(OpenVINOGaitExtractor)
    mask = weakref.ref(value._foreground_cache['camera'][1])
    value.close()
    value.close()
    assert not value.backgrounds and not value._foreground_cache
    assert mask() is None


def test_dlstreamer_gait_close_releases_engine_and_preserves_peer_state():
    first, peer = extractor(DlsGaitExtractor), extractor(DlsGaitExtractor)
    calls = []
    first.engine = SimpleNamespace(close=lambda: calls.append('first-owner'))
    peer.engine = SimpleNamespace(close=lambda: calls.append('peer-owner'))
    peer_mask = peer._foreground_cache['camera'][1]
    first.close()
    assert calls == ['first-owner']
    assert not first.backgrounds and not first._foreground_cache
    assert peer._foreground_cache['camera'][1] is peer_mask
    assert peer.backgrounds
    peer.close()
    assert calls == ['first-owner', 'peer-owner']


def test_engine_is_released_even_if_background_cleanup_raises():
    value = extractor(DlsGaitExtractor)
    def failed():
        raise RuntimeError('controlled cleanup failure')
    value.backgrounds['failing'] = SimpleNamespace(close=failed)
    calls = []
    value.engine = SimpleNamespace(close=lambda: calls.append('released'))
    with pytest.raises(RuntimeError, match='controlled cleanup failure'):
        value.close()
    assert calls == ['released']
    assert not value.backgrounds and not value._foreground_cache


def test_partially_initialized_gait_can_release_backgrounds():
    value = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    value.close_backgrounds()
