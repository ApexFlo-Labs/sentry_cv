"""Self-check a generated diagnostic hook before allowing it in a live worker."""
import hashlib
import json
from pathlib import Path
import types
from unittest.mock import patch

import numpy as np
import cv2  # Preload extension before patch.dict restores the module registry.
import pytest


HOOK = Path(__file__).resolve().parents[2] / 'optimization_evidence/2026-11-10-local-v8-r2/fixtures/compare_scene_vectors_native_hook.py'
pytestmark = pytest.mark.skipif(not HOOK.is_file(), reason='Campaign-generated frozen witness fixture required')


def loaded(tmp_path):
    real = np.arange(1, 769, dtype=np.float32)
    real /= np.linalg.norm(real)
    cache = {}

    class FakeExtractor:
        actual_device = 'GPU/DLStreamer'

        def embed(self, frame):
            return cache.get(hashlib.sha256(frame.tobytes()).hexdigest(), real)

    fake = types.ModuleType('pipeline.dls_secondary')
    fake.DlsSceneExtractor = FakeExtractor
    parent = types.ModuleType('pipeline')
    parent.dls_secondary = fake
    namespace = {'__name__': 'diagnostic_fixture_self_check'}
    with patch.dict('sys.modules', {'pipeline': parent, 'pipeline.dls_secondary': fake}):
        exec(compile(HOOK.read_text(), str(HOOK), 'exec'), namespace)
    for pixels, reference, metadata in namespace['references']:
        cache[metadata['input_pixel_sha256']] = reference
    namespace['Path'] = lambda _: tmp_path
    return namespace, FakeExtractor(), real, cache


def test_hook_preserves_real_vector_skips_warmup_and_serializes_all_eight(tmp_path):
    namespace, engine, real, _ = loaded(tmp_path)
    assert engine.embed(np.zeros((224, 224, 3), np.uint8)) is real
    assert not list(tmp_path.iterdir())
    for _ in range(10):
        assert engine.embed(np.zeros((720, 1280, 3), np.uint8)) is real
    rows = [json.loads(p.read_text()) for p in sorted(tmp_path.glob('*.json'))]
    assert len(rows) == len(namespace['references']) == 8
    assert all(type(r['passed']) is bool and r['passed'] for r in rows)
    assert len(list(tmp_path.glob('*.npy'))) == 8


def test_numeric_failure_is_recorded_without_substituting_reference_into_output(tmp_path):
    namespace, engine, real, cache = loaded(tmp_path)
    _, ref, metadata = namespace['references'][0]
    cache[metadata['input_pixel_sha256']] = -ref
    assert engine.embed(np.zeros((720, 1280, 3), np.uint8)) is real
    row = json.loads((tmp_path/'01.json').read_text())
    assert row['passed'] is False
    assert row['cosine'] < -.99
