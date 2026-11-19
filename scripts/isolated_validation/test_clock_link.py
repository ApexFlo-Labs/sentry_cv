import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('clock_link', Path(__file__).with_name('clock_link.py'))
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


def test_asymmetric_delay_retains_true_offset_without_claiming_exact_latency():
    # Actual offset990; remote sampled at local10, response arrived at10.2.
    s = {'local_before_mono': 10, 'local_after_mono': 10.2,
         'remote': {'monotonic': 1000}}
    result = clock.map_monotonic(20, [s], drift_bound_ppm=0)
    assert result['minimum_remote_monotonic'] <= 1010 <= result['maximum_remote_monotonic']
    assert result['estimated_remote_monotonic'] == pytest.approx(1009.9)
    assert result['uncertainty_seconds'] == pytest.approx(.1)


def test_old_calibration_cannot_be_presented_as_precise_cross_host_latency():
    s = {'local_before_mono': 10, 'local_after_mono': 10.2, 'remote': {'monotonic': 1000}}
    with pytest.raises(ValueError, match='recent'):
        clock.map_monotonic(100, [s], max_probe_age=60)
    r = clock.map_monotonic(20, [s], drift_bound_ppm=2000)
    assert r['uncertainty_seconds'] > .1


@pytest.mark.parametrize('before,after,remote', [(2, 1, 3), (1, 2, float('nan'))])
def test_invalid_probe_is_rejected(before, after, remote):
    with pytest.raises(ValueError):
        clock.map_monotonic(3, [{'local_before_mono': before, 'local_after_mono': after,
                                'remote': {'monotonic': remote}}])
