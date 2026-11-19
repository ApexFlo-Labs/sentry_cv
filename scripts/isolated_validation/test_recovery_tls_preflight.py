from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import ssl
import sys
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import v8_recovery_image_validation as recovery


def test_plain_rtsp_does_not_use_a_tls_probe(monkeypatch):
    monkeypatch.setattr(recovery.socket, 'create_connection', lambda *a, **k: pytest.fail('Unexpected probe'))
    assert recovery.verify_relay_upstream_tls(urlsplit('rtsp://fixture:554/video'), None) == {
        'tls': False, 'verified': None}


def test_tls_preflight_preserves_verification_and_source_hostname(monkeypatch):
    calls = []
    raw = object()
    def connect(address, timeout):
        calls.append((address, timeout));return nullcontext(raw)
    def wrap(value, server_hostname):
        assert value is raw and server_hostname == 'fixture'
        return nullcontext(object())
    monkeypatch.setattr(recovery.socket, 'create_connection', connect)
    assert recovery.verify_relay_upstream_tls(urlsplit('rtsps://fixture:443/video'),
        SimpleNamespace(wrap_socket=wrap)) == {'tls': True, 'verified': True}
    assert calls == [(('fixture',443),8)]


def test_missing_fixture_ca_fails_before_any_cv_work(monkeypatch):
    monkeypatch.setattr(recovery.socket, 'create_connection', lambda *a, **k: nullcontext(object()))
    def rejected(*a, **k):
        raise ssl.SSLCertVerificationError('unknown test CA')
    with pytest.raises(ssl.SSLCertVerificationError, match='unknown test CA'):
        recovery.verify_relay_upstream_tls(urlsplit('rtsps://fixture:443/video'),
            SimpleNamespace(wrap_socket=rejected))
