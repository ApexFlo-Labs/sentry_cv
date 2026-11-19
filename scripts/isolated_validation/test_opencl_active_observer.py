import importlib.util
from pathlib import Path
import struct
import pytest

SPEC = importlib.util.spec_from_file_location('active_trace', Path(__file__).with_name('opencl_active_observer.py'))
trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace)


def make_record(path, sequence=2, magic=b'SCLACT1\0'):
    raw = bytearray(64 + 192)
    trace.HEADER.pack_into(raw, 0, magic, 1, 42, 1, 192, 64, 100, 0)
    trace.SLOT.pack_into(raw, 64, sequence, 51, 20, 19, 1000, 900,
                         1, 0xabc, 500, 200, -36, b'clFinish')
    path.write_bytes(raw)


def test_active_api_age_counters_and_signed_error(tmp_path):
    path = tmp_path / 'trace'
    make_record(path)
    result = trace.snapshot(path, now_ns=2_000_001_000)
    row = result['slots'][0]
    assert row['api'] == 'clFinish' and row['active_age_seconds'] == 2
    assert row['calls'] == 20 and row['completed'] == 19
    assert row['last_status'] == -36 and result['pid'] == 42


def test_concurrent_write_is_not_reported_as_stable(tmp_path):
    path = tmp_path / 'trace'
    make_record(path, sequence=3)
    result = trace.snapshot(path)
    assert result['unstable_slots'] == 1 and result['slots'] == []


def test_unknown_or_truncated_layout_rejected(tmp_path):
    path = tmp_path / 'trace'
    make_record(path, magic=b'UNKNOWN!')
    with pytest.raises(ValueError):
        trace.snapshot(path)
    path.write_bytes(b'bad')
    with pytest.raises(ValueError):
        trace.snapshot(path)
