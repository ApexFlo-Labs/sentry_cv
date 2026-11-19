"""Offline regressions for the pure decoded-boundary timestamp cache."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import weakref

import pytest

from pipeline.frame_time import DecodedFrameClock, FrameOutputTimes


UTC = 1_800_000_000.125
HZ = 1_000_000_000
NONE_PTS = (1 << 64) - 1


def test_normal_decoded_frame_freezes_edge_receive_utc():
    clock = DecodedFrameClock()
    assert clock.timestamp_source == "edge_received_at"
    assert clock.record(123 * HZ, UTC) == UTC
    assert clock.lookup(123 * HZ) == UTC
    assert clock.observe(123 * HZ, UTC, clock_hz=HZ) == UTC
    assert len(clock) == 1
    assert clock.generation == 0


def test_inference_callback_and_send_delays_keep_exact_utc():
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    for delay in (0.001, 3.5, 60, 3600):
        assert clock.observe(10, UTC + delay, HZ) == UTC
    assert clock.record(10, UTC + 7200) == UTC
    assert len(clock) == 1


def test_buffered_burst_cannot_project_pts_into_future_utc():
    clock = DecodedFrameClock()
    # A minute of buffered media arrives in milliseconds, with arbitrary PTS.
    for index in range(60):
        pts = 10**15 + index * HZ
        received = UTC + index / 1000
        assert clock.record(pts, received) == received
        for hz in (1, 90_000, HZ):
            assert clock.observe(pts, received, hz) == received


def test_primary_and_fire_share_same_pts_timestamp():
    clock = DecodedFrameClock()
    generation, stamp = clock.record_with_generation(100, UTC)
    primary = clock.observe(100, UTC + 1, HZ, generation=generation)
    fire = clock.observe(100, UTC + 20, HZ, generation=generation)
    assert primary == fire == stamp == UTC


def test_later_candidates_retain_stamp_and_reads_never_reset_epoch():
    clock = DecodedFrameClock()
    generation, stamp = clock.record_with_generation(100, UTC)
    clock.record(200, UTC + 1)
    clock.record(300, UTC + 2)
    for pts in (300, 100, 200, 100):
        expected = {100: stamp, 200: UTC + 1, 300: UTC + 2}[pts]
        assert clock.observe(pts, UTC + 500, HZ, generation=generation) == expected
    assert clock.generation == generation
    assert len(clock) == 3


def test_missing_timestamp_never_anchors_from_callback_or_arbitrary_pts():
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    for pts in (0, 11, 10**18):
        with pytest.raises(LookupError, match="no decoded receive timestamp"):
            clock.observe(pts, UTC + 3600, HZ)
    assert len(clock) == 1
    assert clock.generation == 0
    assert clock.lookup(10) == UTC


def test_evicted_timestamp_is_never_replaced_with_now():
    clock = DecodedFrameClock(max_entries=1)
    clock.record(10, UTC)
    clock.record(20, UTC + 1)
    for _ in range(3):
        with pytest.raises(LookupError):
            clock.observe(10, UTC + 3600, HZ)
        with pytest.raises(LookupError):
            clock.lookup(10)
    assert len(clock) == 1
    assert clock.lookup(20) == UTC + 1


def test_fifo_retention_does_not_refresh_on_read_or_duplicate():
    clock = DecodedFrameClock(max_entries=2)
    clock.record(10, UTC)
    clock.record(20, UTC + 1)
    assert clock.lookup(10) == UTC
    assert clock.record(20, UTC + 99) == UTC + 1
    clock.record(30, UTC + 2)
    with pytest.raises(LookupError):
        clock.lookup(10)
    assert clock.lookup(20) == UTC + 1
    assert clock.lookup(30) == UTC + 2
    assert len(clock) == clock.max_entries == 2


def test_default_retention_is_2048_scalar_entries_in_ordered_dict():
    clock = DecodedFrameClock()
    assert clock.max_entries == 2048
    for pts in range(2200):
        clock.record(pts, UTC + pts)
        assert len(clock) <= 2048
    assert len(clock) == 2048
    assert type(clock._entries) is OrderedDict
    assert all(type(k) is int and type(v) is float for k, v in clock._entries.items())
    with pytest.raises(LookupError):
        clock.lookup(151)
    assert clock.lookup(152) == UTC + 152
    assert clock.lookup(2199) == UTC + 2199


def test_backwards_pts_starts_epoch_even_when_old_pts_is_still_cached():
    clock = DecodedFrameClock()
    old_generation, _ = clock.record_with_generation(100, UTC)
    clock.record(200, UTC + 1)
    new_generation, stamp = clock.record_with_generation(100, UTC + 10)
    assert new_generation == old_generation + 1
    assert stamp == UTC + 10
    assert len(clock) == 1
    with pytest.raises(LookupError):
        clock.lookup(200)
    with pytest.raises(LookupError, match="stale"):
        clock.lookup(100, generation=old_generation)
    assert clock.lookup(100, generation=new_generation) == UTC + 10


def test_explicit_reset_rejects_old_session_reusing_same_pts():
    clock = DecodedFrameClock()
    generation, _ = clock.record_with_generation(100, UTC)
    assert clock.reset() == generation + 1
    assert len(clock) == 0
    with pytest.raises(LookupError):
        clock.lookup(100)
    current_generation, stamp = clock.record_with_generation(100, UTC + 10)
    for read in (
        lambda: clock.lookup(100, generation=generation),
        lambda: clock.observe(100, UTC + 20, HZ, generation=generation),
    ):
        with pytest.raises(LookupError, match="stale"):
            read()
    assert clock.lookup(100, generation=current_generation) == stamp == UTC + 10
    assert clock.reset() == current_generation + 1
    assert clock.reset() == current_generation + 2


def test_reset_handles_reconnect_with_increasing_pts():
    clock = DecodedFrameClock()
    clock.record(100, UTC)
    generation = clock.reset()
    assert clock.record_with_generation(1000, UTC + 100) == (generation, UTC + 100)
    with pytest.raises(LookupError):
        clock.lookup(100)


def test_threads_read_primary_fire_during_decode_and_eviction():
    clock = DecodedFrameClock(max_entries=3)
    rendezvous = Barrier(3)

    def decode():
        for pts in range(100):
            assert clock.record_with_generation(pts, UTC + pts) == (0, UTC + pts)
            rendezvous.wait(timeout=5)
            rendezvous.wait(timeout=5)

    def consume(delay):
        stamps = []
        for pts in range(100):
            rendezvous.wait(timeout=5)
            stamps.append(clock.observe(pts, UTC + pts + delay, HZ, generation=0))
            if pts >= 3:
                with pytest.raises(LookupError):
                    clock.lookup(pts - 3, generation=0)
            assert len(clock) <= 3
            rendezvous.wait(timeout=5)
        return stamps

    with ThreadPoolExecutor(max_workers=3) as pool:
        producer = pool.submit(decode)
        primary = pool.submit(consume, 1)
        fire = pool.submit(consume, 30)
        producer.result(timeout=15)
        assert primary.result(timeout=15) == fire.result(timeout=15) == [
            UTC + pts for pts in range(100)
        ]


def test_concurrent_duplicate_records_freeze_one_original_stamp():
    clock = DecodedFrameClock()
    rendezvous = Barrier(8)

    def record(index):
        rendezvous.wait(timeout=5)
        return clock.record_with_generation(100, UTC + index)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(record, range(8)))
    assert len(set(results)) == 1
    generation, stamp = results[0]
    assert generation == 0
    assert stamp in [UTC + index for index in range(8)]
    assert clock.lookup(100) == stamp
    assert len(clock) == 1


def test_old_callback_interleaved_with_reset_and_same_pts_is_rejected():
    clock = DecodedFrameClock()
    recorded = Event()
    replaced = Event()

    def old_callback():
        generation, stamp = clock.record_with_generation(100, UTC)
        recorded.set()
        assert replaced.wait(timeout=5)
        with pytest.raises(LookupError, match="stale"):
            clock.observe(100, UTC + 100, HZ, generation=generation)
        return stamp

    def reconnect():
        assert recorded.wait(timeout=5)
        clock.reset()
        result = clock.record_with_generation(100, UTC + 10)
        replaced.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(old_callback)
        new = pool.submit(reconnect)
        assert old.result(timeout=10) == UTC
        assert new.result(timeout=10) == (1, UTC + 10)


INVALID_PTS = [-1, NONE_PTS, NONE_PTS + 1, 1.5, float("nan"), float("inf"), None, "1", True]
INVALID_TIME = [-1.0, float("nan"), float("inf"), -float("inf"), None, "1", True, 10**1000]


@pytest.mark.parametrize("pts", INVALID_PTS)
def test_invalid_pts_rejected_without_mutating_cache(pts):
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    for call in (
        lambda: clock.record(pts, UTC),
        lambda: clock.record_with_generation(pts, UTC),
        lambda: clock.lookup(pts),
        lambda: clock.observe(pts, UTC, HZ),
    ):
        with pytest.raises(ValueError):
            call()
    assert clock.generation == 0
    assert len(clock) == 1
    assert clock.lookup(10) == UTC


@pytest.mark.parametrize("received_at", INVALID_TIME)
def test_invalid_timestamp_rejected_even_for_cached_pts(received_at):
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    for call in (
        lambda: clock.record(10, received_at),
        lambda: clock.record(0, received_at),  # Invalid input must not reset.
        lambda: clock.observe(10, received_at, HZ),
    ):
        with pytest.raises(ValueError):
            call()
    assert clock.generation == 0
    assert len(clock) == 1
    assert clock.lookup(10) == UTC


@pytest.mark.parametrize("clock_hz", [0, *INVALID_TIME])
def test_invalid_clock_rate_rejected_without_replacing_stamp(clock_hz):
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    with pytest.raises(ValueError):
        clock.observe(10, UTC + 100, clock_hz)
    assert clock.lookup(10) == UTC


@pytest.mark.parametrize("size", [0, -1, 1.5, float("nan"), float("inf"), None, "2", True])
def test_invalid_cache_size_rejected(size):
    with pytest.raises(ValueError):
        DecodedFrameClock(max_entries=size)


@pytest.mark.parametrize("generation", [-1, 0.5, float("nan"), float("inf"), "0", True])
def test_invalid_generation_rejected(generation):
    clock = DecodedFrameClock()
    clock.record(10, UTC)
    with pytest.raises(ValueError):
        clock.lookup(10, generation=generation)
    with pytest.raises(ValueError):
        clock.observe(10, UTC, HZ, generation=generation)


def test_zero_pts_zero_utc_and_highest_valid_pts_are_not_missing():
    clock = DecodedFrameClock()
    assert clock.record(0, 0.0) == 0.0
    assert clock.record(0, UTC) == 0.0
    assert clock.observe(0, UTC, HZ) == 0.0
    assert clock.record(NONE_PTS - 1, UTC) == UTC
    assert clock.lookup(NONE_PTS - 1) == UTC


def test_output_times_common_decoder_stamp_is_used_without_clock_lookup():
    clock = DecodedFrameClock(max_entries=1)
    outputs = FrameOutputTimes()
    generation, stamp = clock.record_with_generation(100, UTC)
    outputs.record(100, clock, generation, stamp)
    clock.record(200, UTC + 1)
    with pytest.raises(LookupError):
        clock.lookup(100)
    assert outputs.consume(100, clock, generation) == UTC


def test_output_times_old_new_clock_same_pts_is_ambiguous():
    old_clock, new_clock = DecodedFrameClock(), DecodedFrameClock()
    outputs = FrameOutputTimes()
    outputs.record(100, old_clock, 0, UTC)
    outputs.record(100, new_clock, 0, UTC + 10)
    for clock in (new_clock, old_clock):
        with pytest.raises(LookupError, match="ambiguous"):
            outputs.consume(100, clock, 0)
    # Neither rejection nor repeated new decoder records can revive this PTS.
    outputs.record(100, new_clock, 0, UTC + 100)
    with pytest.raises(LookupError, match="ambiguous"):
        outputs.consume(100, new_clock, 0)
    assert len(outputs) == 1
    assert outputs._entries[100] is None


def test_output_times_same_clock_reused_pts_in_new_epoch_is_ambiguous():
    clock = DecodedFrameClock()
    outputs = FrameOutputTimes()
    generation, stamp = clock.record_with_generation(100, UTC)
    outputs.record(100, clock, generation, stamp)
    clock.record(200, UTC + 1)
    new_generation, stamp = clock.record_with_generation(100, UTC + 10)
    outputs.record(100, clock, new_generation, stamp)
    for epoch in (generation, new_generation):
        with pytest.raises(LookupError, match="ambiguous"):
            outputs.consume(100, clock, epoch)


@pytest.mark.parametrize("replace_clock", [False, True])
def test_output_times_stale_dispatch_rejected_and_quarantined(replace_clock):
    clock = DecodedFrameClock()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    current = DecodedFrameClock() if replace_clock else clock
    generation = 0 if replace_clock else clock.reset()
    with pytest.raises(LookupError, match="stale"):
        outputs.consume(100, current, generation)
    with pytest.raises(LookupError, match="ambiguous"):
        outputs.consume(100, clock, 0)
    outputs.record(100, current, generation, UTC + 10)
    with pytest.raises(LookupError):
        outputs.consume(100, current, generation)


def test_output_times_consuming_twice_is_missing_and_duplicate_cannot_rearm():
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    assert outputs.consume(100, clock, 0) == UTC
    with pytest.raises(LookupError, match="no unconsumed"):
        outputs.consume(100, clock, 0)
    outputs.record(100, clock, 0, UTC + 100)
    with pytest.raises(LookupError, match="no unconsumed"):
        outputs.consume(100, clock, 0)
    outputs.record(100, object(), 0, UTC + 200)
    with pytest.raises(LookupError, match="ambiguous"):
        outputs.consume(100, clock, 0)


def test_output_times_same_epoch_duplicate_preserves_first_stamp():
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 7, UTC)
    outputs.record(200, clock, 7, UTC + 1)
    outputs.record(100, clock, 7, UTC + 100)
    assert len(outputs) == 2
    assert outputs.consume(100, clock, 7) == UTC
    assert outputs.consume(200, clock, 7) == UTC + 1


def test_output_times_compares_clock_identity_without_equality():
    class Clock:
        def __eq__(self, other):
            raise AssertionError("clock identity must use is")

    old_clock, new_clock = Clock(), Clock()
    outputs = FrameOutputTimes()
    outputs.record(100, old_clock, 0, UTC)
    outputs.record(100, old_clock, 0, UTC + 1)
    outputs.record(100, new_clock, 0, UTC + 10)
    with pytest.raises(LookupError, match="ambiguous"):
        outputs.consume(100, new_clock, 0)


def test_output_times_eviction_is_fifo_and_missing_reads_do_not_add_state():
    clock = object()
    outputs = FrameOutputTimes(max_entries=2)
    outputs.record(100, clock, 0, UTC)
    outputs.record(200, clock, 0, UTC + 1)
    outputs.record(100, clock, 0, UTC + 100)
    for pts in range(1000, 1100):
        with pytest.raises(LookupError, match="no output dispatch"):
            outputs.consume(pts, clock, 0)
        assert len(outputs) == 2
    outputs.record(300, clock, 0, UTC + 2)
    with pytest.raises(LookupError, match="no output dispatch"):
        outputs.consume(100, clock, 0)
    assert len(outputs) == outputs.max_entries == 2
    assert outputs.consume(200, clock, 0) == UTC + 1
    assert outputs.consume(300, clock, 0) == UTC + 2


def test_output_times_out_of_order_consumption_preserves_other_records():
    clock = object()
    outputs = FrameOutputTimes()
    for pts in (100, 200, 300):
        outputs.record(pts, clock, 0, UTC + pts)
    for pts in (300, 100, 200):
        assert outputs.consume(pts, clock, 0) == UTC + pts


def test_output_times_default_bound_includes_consumed_and_ambiguous_markers():
    clock = object()
    outputs = FrameOutputTimes()
    assert outputs.max_entries == 2048
    for pts in range(2200):
        outputs.record(pts, clock, 0, UTC + pts)
        if pts % 2:
            outputs.consume(pts, clock, 0)
        else:
            outputs.record(pts, clock, 1, UTC + pts + 1)
        assert len(outputs) <= 2048
    assert len(outputs) == 2048
    assert type(outputs._entries) is OrderedDict
    with pytest.raises(LookupError, match="no output dispatch"):
        outputs.consume(151, clock, 0)
    with pytest.raises(LookupError, match="ambiguous"):
        outputs.consume(152, clock, 0)
    with pytest.raises(LookupError, match="no unconsumed"):
        outputs.consume(153, clock, 0)


def test_output_times_eviction_releases_old_source_clock_reference():
    class Clock:
        pass

    outputs = FrameOutputTimes(max_entries=1)
    old_clock = Clock()
    reference = weakref.ref(old_clock)
    outputs.record(100, old_clock, 0, UTC)
    del old_clock
    assert reference() is not None
    outputs.record(200, Clock(), 0, UTC + 1)
    assert reference() is None


def test_output_times_concurrent_consumers_only_one_gets_stamp():
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    rendezvous = Barrier(8)

    def consume():
        rendezvous.wait(timeout=5)
        try:
            return outputs.consume(100, clock, 0)
        except LookupError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume(), range(8)))
    assert results.count(UTC) == 1
    assert results.count(None) == 7


@pytest.mark.parametrize("pts", INVALID_PTS)
def test_output_times_invalid_pts_rejected_without_mutation(pts):
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    with pytest.raises(ValueError):
        outputs.record(pts, clock, 0, UTC + 1)
    with pytest.raises(ValueError):
        outputs.consume(pts, clock, 0)
    assert len(outputs) == 1
    assert outputs.consume(100, clock, 0) == UTC


@pytest.mark.parametrize("stamp", INVALID_TIME)
def test_output_times_invalid_stamp_rejected_without_mutation(stamp):
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    with pytest.raises(ValueError):
        outputs.record(100, object(), 1, stamp)
    assert outputs.consume(100, clock, 0) == UTC


@pytest.mark.parametrize("generation", [-1, 0.5, float("nan"), float("inf"), None, "0", True])
def test_output_times_invalid_generation_rejected_without_mutation(generation):
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(100, clock, 0, UTC)
    with pytest.raises(ValueError):
        outputs.record(100, clock, generation, UTC + 1)
    with pytest.raises(ValueError):
        outputs.consume(100, clock, generation)
    assert outputs.consume(100, clock, 0) == UTC


@pytest.mark.parametrize("size", [0, -1, 1.5, float("nan"), float("inf"), None, "2", True])
def test_output_times_invalid_capacity_rejected(size):
    with pytest.raises(ValueError):
        FrameOutputTimes(max_entries=size)


def test_output_times_zero_stamp_is_not_missing():
    clock = object()
    outputs = FrameOutputTimes()
    outputs.record(0, clock, 0, 0.0)
    assert outputs.consume(0, clock, 0) == 0.0
