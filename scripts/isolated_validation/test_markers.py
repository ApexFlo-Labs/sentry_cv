"""Offline independent-marker regressions; no media producer or model runs."""
import importlib.util
import json
from pathlib import Path
import numpy as np
import cv2
import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


markers = module('analyze_markers')
builder = module('make_marked_fixture')


@pytest.mark.parametrize('shape', [(720, 1280), (1080, 1920)])
@pytest.mark.parametrize('index', [0, 127, 1199, 8192])
def test_checksum_marker_survives_jpeg_at_source_geometry(shape, index):
    image = builder.paint(np.zeros((*shape, 3), dtype=np.uint8), index)
    ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    assert markers.read_marker(image, shape[1], shape[0]-14) == index


def test_corrupt_marker_is_unknown_not_wrong_frame():
    image = builder.paint(np.zeros((720, 1280, 3), dtype=np.uint8), 123)
    image[706, 14+12*55] = 255 - image[706, 14+12*55]
    assert markers.read_marker(image) is None


def test_ambiguous_repeated_transmission_is_not_latest_match():
    rows = [{'camera_id':'a', 'stream_session_id':'s', 'frame_ordinal':1,
             'marker':0, 'accepted_mono':300}]
    packets = {'a':{0:{'source_index':0,'marker_index':0,'transmitted_mono':1},
                    100:{'source_index':100,'marker_index':0,'transmitted_mono':101}}}
    result, counts = markers.align(rows, packets, {'a':(10,100)})
    assert not result
    assert counts['ambiguous_repeat'] == 1


def test_current_metadata_gallery_and_uncompressed_log(tmp_path):
    gallery = tmp_path/'gallery'/'package';gallery.mkdir(parents=True)
    epochs = tmp_path/'sender-epochs';epochs.mkdir()
    fixture = tmp_path/'marked.mp4'
    fixture.with_suffix('.json').write_text(json.dumps({'width':1280, 'marker_y':706}))
    image = builder.paint(np.zeros((720,1280,3),dtype=np.uint8), 5)
    cv2.imwrite(str(gallery/'evidence_0.jpg'), image)
    record = dict(package_id='package', camera_id='capacity-01', stream_session_id='s',
                  event_type='scene_sample', source_frame_id='capacity-01:10',
                  observed_at='2026-11-10T00:00:00Z', accepted_at='2026-11-10T00:00:01Z',
                  accepted_mono=10, evidence_metadata=[{'part_name':'evidence_0'}])
    (gallery/'metadata.json').write_text(json.dumps(record))
    (tmp_path/'packages.jsonl').write_text(json.dumps(record)+'\n')
    (tmp_path/'report.json').write_text(json.dumps({'samples':[
        {'sample_phase':'steady','sample_monotonic_seconds':0},
        {'sample_phase':'steady','sample_monotonic_seconds':20}]}))
    (epochs/'19001.json').write_text(json.dumps({'source_fps':10, 'source_frames':1200,
                                               'fixture':str(fixture)}))
    (epochs/'19001-pes.jsonl').write_text(json.dumps({
        'source_index':5,'marker_index':5,'transmitted_mono':9.5})+'\n')
    result = markers.analyze(tmp_path/'gallery', epochs)
    assert result['steady_per_camera_ms']['capacity-01']['p50'] == 500
    assert result['counts']['marker_decoded'] == 1
