"""Bounded cross-host monotonic mapping for test sources, never runtime clocks."""
import math


def map_monotonic(value, samples, max_probe_age=60, drift_bound_ppm=2000):
    """Return an estimate/interval, conditional on the declared clock drift bound.

    SSH timestamps bound offset without assuming symmetric network delay. The
    additional drift allowance is explicit; this does not establish NTP accuracy
    or source-camera capture time. Reject distant or malformed calibration data.
    """
    if not math.isfinite(value) or max_probe_age <= 0 or drift_bound_ppm < 0:
        raise ValueError('invalid clock mapping parameters')
    choices = []
    for s in samples:
        before, after = s['local_before_mono'], s['local_after_mono']
        remote = s['remote']['monotonic']
        if not all(math.isfinite(v) for v in (before, after, remote)) or after < before:
            raise ValueError('invalid clock probe interval')
        midpoint = (before+after)/2
        distance = abs(value-midpoint)
        if distance > max_probe_age:
            continue
        extra = drift_bound_ppm/1e6 * (distance+(after-before)/2)
        choices.append((after-before+2*extra, remote-after-extra,
                        remote-before+extra, midpoint, distance))
    if not choices:
        raise ValueError('no recent bounded clock probe')
    _, low, high, probe, distance = min(choices)
    return {'estimated_remote_monotonic': value+(low+high)/2,
            'minimum_remote_monotonic': value+low,
            'maximum_remote_monotonic': value+high,
            'uncertainty_seconds': (high-low)/2,
            'clock_probe_local_midpoint': probe, 'distance_to_probe_seconds': distance,
            'assumed_maximum_relative_drift_ppm': drift_bound_ppm,
            'scope': 'Conditional interval from SSH request bounds and explicit drift allowance'}
