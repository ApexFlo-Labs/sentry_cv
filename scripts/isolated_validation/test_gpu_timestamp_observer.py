import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('gpu_observer', Path(__file__).with_name('gpu_timestamp_observer.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_partial_nested_objects_strings_and_array_wrapping():
    p = module.Objects()
    assert p.feed('[\n{"engines":{"Compute":{"busy":') == []
    assert p.feed('24.25}},"name":"text } bracket ["},\n') == [
        {'engines': {'Compute': {'busy': 24.25}}, 'name': 'text } bracket ['}]
    assert p.feed('{"period":{"duration":1000}}]\n') == [{'period': {'duration': 1000}}]
    assert p.pending == ''


def test_multiple_objects_and_partial_tail_keep_values():
    p = module.Objects()
    assert p.feed('{"busy":0}\n{"busy":100}\n{"busy":') == [{'busy': 0}, {'busy': 100}]
    assert p.pending == '{"busy":'
    assert p.feed('12.5}') == [{'busy': 12.5}]


def test_unexpected_scalar_and_memory_bound_fail_explicitly():
    with pytest.raises(ValueError):
        module.Objects().feed('true')
    with pytest.raises(ValueError):
        module.Objects().feed('x'*(16*1024*1024+1))
