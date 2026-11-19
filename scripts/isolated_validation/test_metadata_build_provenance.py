"""A generic rebuild must not silently substitute stale helper bytes or ABI."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('metadata_provenance', ROOT / 'native/verify_gst_metadata.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(root):
    manifest = {'gst_analytics_version': '1.28.2'}
    for name, key, content in [
        ('src/gst_detection_metadata.c', 'source_sha256', b'fixture source'),
        ('lib/libsentinel_gst_metadata.so', 'binary_sha256', b'fixture binary')
    ]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest[key] = hashlib.sha256(content).hexdigest()
    path = root / 'build-info/gst_metadata.json'
    path.parent.mkdir()
    path.write_text(json.dumps(manifest))
    return path, manifest


def test_verified_pair_is_accepted(tmp_path):
    _, manifest = fixture(tmp_path)
    assert module.verify(tmp_path) == manifest


@pytest.mark.parametrize('name', ['src/gst_detection_metadata.c', 'lib/libsentinel_gst_metadata.so'])
def test_changed_source_or_binary_cannot_use_old_qualification(tmp_path, name):
    fixture(tmp_path)
    (tmp_path / name).write_bytes(b'changed after qualification')
    with pytest.raises(ValueError, match='provenance mismatch'):
        module.verify(tmp_path)


def test_older_host_sdk_cannot_replace_qualified_sdk(tmp_path):
    path, manifest = fixture(tmp_path)
    manifest['gst_analytics_version'] = '1.24.2'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='SDK GstAnalytics'):
        module.verify(tmp_path)
