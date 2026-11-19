"""Compilation controls cannot silently alter inference/device policy."""
import pytest
from pipeline.gva_inference_options import gpu_compilation_ie_config, native_fire_device


@pytest.mark.parametrize('device',['GPU','GPU.0','GPU.0.1','MULTI:GPU,NPU'])
def test_gpu_containing_targets_bound_compilation_only(monkeypatch,device):
    monkeypatch.delenv('SENTINEL_GVA_GPU_COMPILATION_THREADS',raising=False)
    assert gpu_compilation_ie_config(device)=='ie-config="COMPILATION_NUM_THREADS=1" '


@pytest.mark.parametrize('device',['NPU','CPU','MULTI:NPU,CPU','GPUMOCK'])
def test_other_targets_not_reconfigured(monkeypatch,device):
    monkeypatch.setenv('SENTINEL_GVA_GPU_COMPILATION_THREADS','invalid')
    assert gpu_compilation_ie_config(device)==''


@pytest.mark.parametrize('value',['0','-1','257','1.5','True','invalid'])
def test_invalid_compiler_override_rejected(monkeypatch,value):
    monkeypatch.setenv('SENTINEL_GVA_GPU_COMPILATION_THREADS',value)
    with pytest.raises(ValueError):gpu_compilation_ie_config('GPU')


def test_explicit_control_keeps_baked_scheduling_comparable(monkeypatch):
    monkeypatch.setenv('SENTINEL_GVA_GPU_COMPILATION_THREADS','16')
    assert gpu_compilation_ie_config('GPU')=='ie-config="COMPILATION_NUM_THREADS=16" '


def test_native_fire_defaults_to_explicit_npu_without_gpu_only_flags(monkeypatch):
    monkeypatch.delenv('SENTINEL_GVA_FIRE_DEVICE', raising=False)
    monkeypatch.setenv('SENTINEL_GVA_GPU_COMPILATION_THREADS', 'invalid')
    assert native_fire_device() == 'NPU'
    assert gpu_compilation_ie_config(native_fire_device()) == ''


@pytest.mark.parametrize('value', ['CPU', 'AUTO', 'MULTI:GPU,NPU', '', 'GPU,NPU'])
def test_native_fire_rejects_implicit_or_cpu_fallback(monkeypatch, value):
    monkeypatch.setenv('SENTINEL_GVA_FIRE_DEVICE', value)
    with pytest.raises(ValueError, match='must be GPU or NPU'):
        native_fire_device()


def test_native_fire_explicit_gpu_control_keeps_existing_compile_limit(monkeypatch):
    monkeypatch.setenv('SENTINEL_GVA_FIRE_DEVICE', 'GPU')
    monkeypatch.setenv('SENTINEL_GVA_GPU_COMPILATION_THREADS', '1')
    assert native_fire_device() == 'GPU'
    assert gpu_compilation_ie_config(native_fire_device()) == 'ie-config="COMPILATION_NUM_THREADS=1" '
