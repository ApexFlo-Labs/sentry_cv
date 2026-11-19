import gzip
import hashlib
import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location('opencl_analysis', Path(__file__).with_name('analyze_opencl_calls.py'))
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def test_nested_api_calls_thread_separation_and_lossless_archive(tmp_path):
    raw = (b'>>>> Time: 10 TID = 1 clFinish: queue\n'
           b'>>>> Time: 12 TID = 1 clFlush: nested\n'
           b'>>>> Time: 13 TID = 2 clWaitForEvents: event\n'
           b'<<<< Time: 20 TID = 1 clFlush -> CL_SUCCESS\n'
           b'<<<< Time: 30 TID = 1 clFinish -> CL_SUCCESS\n')
    source = tmp_path / 'raw'
    source.write_bytes(raw)
    target = tmp_path / 'archive.gz'
    result = analysis.analyze(source, tmp_path / 'analysis.json', target, rate=0)
    assert gzip.decompress(target.read_bytes()) == raw
    assert result['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['api_counts_and_call_wall_time']['clFinish']['max_wall_us'] == 20
    assert result['api_counts_and_call_wall_time']['clFlush']['max_wall_us'] == 8
    assert result['outstanding_at_file_end'][0]['tid'] == 2
    assert result['outstanding_at_file_end'][0]['age_at_last_api_log_us'] == 17
    with pytest.raises(ValueError, match='fresh output'):
        analysis.analyze(source, tmp_path / 'analysis.json', target, rate=0)


def test_deliberate_thermal_pause_not_a_native_deadlock():
    spec = importlib.util.spec_from_file_location('capture_stall', Path(__file__).with_name('capture_native_stall.py'))
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    assert capture.deliberately_paused({'status':'PAUSED_STARTUP_COOLING'}, now=20)
    record = {'status':'RUNNING','startup_pauses':[{'end_monotonic':20}]}
    assert capture.deliberately_paused(record, now=24.9)
    assert not capture.deliberately_paused(record, now=25)
