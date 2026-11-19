"""HTTP admission must reach an explicit VA decoder, not the file/RTSP branch."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline.surface_stream import _http_surface_source, _probe_source, UnsupportedSurfaceSource


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('codec,expected', [('h264', 'h264'), ('hevc', 'h265')])
def test_http_probe_preserves_codec_and_geometry_checks(monkeypatch, scheme, codec, expected):
    calls = []
    def probe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
            {'streams': [{'codec_name': codec, 'width': 1280, 'height': 720}]}))
    monkeypatch.setattr('subprocess.run', probe)
    uri = scheme+'://127.0.0.1:9011/video.ts'
    assert _probe_source(uri, require_fixed_surface=False) == expected
    assert calls[0][-1] == uri
    with pytest.raises(UnsupportedSurfaceSource, match='1920x1080'):
        _probe_source(uri, require_fixed_surface=True)


@pytest.mark.parametrize('codec', ['h264', 'h265'])
@pytest.mark.parametrize('prefix', ['va', 'vaapi'])
def test_http_graph_keeps_encoded_caps_and_hardware_decoder(codec, prefix):
    graph = _http_surface_source('http://127.0.0.1:9011/video.ts', codec, prefix+codec+'dec')
    assert f'video/x-{codec}' in graph
    assert f'! {prefix}{codec}dec name=decoder' in graph
    assert 'parse-streams=true' in graph and 'use-buffering=false' in graph
    assert 'decodebin' not in graph and 'filesrc' not in graph and 'rtspsrc' not in graph


@pytest.mark.parametrize('uri,codec,decoder', [
    ('http:///missing-host', 'h264', 'vah264dec'),
    ('file:///video.ts', 'h264', 'vah264dec'),
    ('http://127.0.0.1/video.ts', 'h264', 'avdec_h264'),
    ('http://127.0.0.1/video.ts', 'mjpeg', 'vajpegdec'),
])
def test_http_source_cannot_select_a_software_decoder(uri, codec, decoder):
    with pytest.raises(UnsupportedSurfaceSource):
        _http_surface_source(uri, codec, decoder)
