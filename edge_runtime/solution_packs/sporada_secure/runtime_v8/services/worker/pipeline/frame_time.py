"""Bounded edge receive timestamps for one camera's decoded media stream.

Call ``record`` at the common decoded boundary, before inference or branching.
Only that boundary writes, in decode order; primary/fire/candidate consumers
use ``lookup`` or the lookup-only ``observe`` compatibility interface. Missing
timestamps raise LookupError so the caller can drop the unevaluated sample.

PTS identifies a frame; it is never converted to absolute UTC. ``received_at``
must be UTC Unix seconds sampled at that boundary, not an inference, callback,
or send time. This is edge receive provenance, with no camera/NTP claim.
"""

from collections import OrderedDict
from math import isfinite
from numbers import Integral, Real
from threading import Lock


_GST_CLOCK_TIME_NONE = (1 << 64) - 1


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return int(value)


def _valid_pts(pts: int) -> int:
    pts = _nonnegative_integer(pts, "PTS")
    if pts >= _GST_CLOCK_TIME_NONE:
        raise ValueError("PTS must be below GST_CLOCK_TIME_NONE")
    return pts


def _finite_nonnegative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite and nonnegative") from exc
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


class DecodedFrameClock:
    """Thread-safe FIFO of PTS -> frozen UTC Unix seconds; no frame retention.

    A lower *recorded* PTS starts a new epoch, even if that PTS is cached.
    Recording the current PTS again preserves its original timestamp. Reads
    may arrive in any order and never reset the epoch or refresh retention.

    For asynchronous work that can outlive a reset, use
    ``generation, stamp = clock.record_with_generation(pts, received_at)`` at
    decode and carry that generation through ``lookup``/``observe``. Capture
    it atomically this way, rather than reading ``generation`` after recording.
    Untagged lookups cannot distinguish old/new sessions sharing the same PTS.
    Explicit reconnects must call ``reset`` even if the new PTS is increasing.
    """

    timestamp_source = "edge_received_at"

    def __init__(self, max_entries: int = 2048) -> None:
        self._max_entries = _nonnegative_integer(max_entries, "max_entries")
        if self._max_entries == 0:
            raise ValueError("max_entries must be positive")
        self._entries: OrderedDict[int, float] = OrderedDict()
        self._last_pts: int | None = None
        self._generation = 0
        self._lock = Lock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def __len__(self) -> int:
        """Number of retained scalar timestamps (not images or surfaces)."""
        with self._lock:
            return len(self._entries)

    def record(self, pts: int, received_at: float) -> float:
        """Freeze this decoded frame's receive time; return the stored UTC."""
        return self.record_with_generation(pts, received_at)[1]

    def record_with_generation(
        self, pts: int, received_at: float
    ) -> tuple[int, float]:
        """Atomically record and return ``(generation, frozen_utc)``."""
        pts = _valid_pts(pts)
        received_at = _finite_nonnegative(received_at, "received_at")
        with self._lock:
            if self._last_pts is not None and pts < self._last_pts:
                self._entries.clear()
                self._generation += 1
            self._last_pts = pts
            if pts not in self._entries:
                if len(self._entries) == self._max_entries:
                    self._entries.popitem(last=False)
                self._entries[pts] = received_at
            return self._generation, self._entries[pts]

    def lookup(self, pts: int, *, generation: int | None = None) -> float:
        """Return only a cached UTC; reject missing/evicted or stale frames."""
        pts = _valid_pts(pts)
        if generation is not None:
            generation = _nonnegative_integer(generation, "generation")
        with self._lock:
            if generation is not None and generation != self._generation:
                raise LookupError(f"stale decoded frame generation {generation}")
            try:
                return self._entries[pts]
            except KeyError:
                raise LookupError(f"no decoded receive timestamp for PTS {pts}") from None

    def observe(
        self,
        pts: int,
        received_at: float,
        clock_hz: float,
        *,
        generation: int | None = None,
    ) -> float:
        """Lookup only; compatibility arguments never anchor or replace UTC.

        ``received_at`` and ``clock_hz`` are validated but do not participate
        in timestamp selection. Callers must drop the sample on LookupError.
        """
        _finite_nonnegative(received_at, "received_at")
        if _finite_nonnegative(clock_hz, "clock_hz") == 0:
            raise ValueError("clock_hz must be positive")
        return self.lookup(pts, generation=generation)

    def reset(self) -> int:
        """Clear retained timestamps and return the new media generation."""
        with self._lock:
            self._entries.clear()
            self._last_pts = None
            self._generation += 1
            return self._generation


class FrameOutputTimes:
    """Bounded dispatch ledger for outputs carrying only their original PTS.

    Keep one ledger across source clock replacements and media epoch resets.
    At the common decoder probe, before branch queues, for *every* frame::

        generation, stamp = clock.record_with_generation(pts, received_at)
        output_times.record(pts, clock, generation, stamp)

    At fire output, snapshot the current clock and generation, then use::

        stamp = output_times.consume(pts, current_clock, current_generation)

    Drop output on LookupError. Never recover with a new clock lookup or now.
    Clock identity is compared with ``is``; clocks need no specific interface.
    The caller supplies the current generation from its source/session state.

    Consumed records retain their identity but no stamp, preventing duplicates
    from rearming them. Cross-clock/epoch collisions discard identity and stamp
    and quarantine that PTS. Both markers count toward the same FIFO bound and
    are not refreshed by reads or duplicates. Collision detection covers only
    retained history; an evicted dispatch cannot be distinguished from a later
    reuse by PTS alone. No pixels or surfaces are retained.
    """

    def __init__(self, max_entries: int = 2048) -> None:
        self._max_entries = _nonnegative_integer(max_entries, "max_entries")
        if self._max_entries == 0:
            raise ValueError("max_entries must be positive")
        # Tuple: (clock identity/reference, generation, stamp or consumed None).
        # A bare None is an ambiguous PTS, quarantined until FIFO eviction.
        self._entries: OrderedDict[int, tuple[object, int, float | None] | None] = OrderedDict()
        self._lock = Lock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        """Total retained entries, including consumed and ambiguous markers."""
        with self._lock:
            return len(self._entries)

    def record(
        self, pts: int, clock_obj: object, generation: int, stamp: float
    ) -> None:
        """Save the first dispatch stamp, or quarantine a conflicting PTS."""
        pts = _valid_pts(pts)
        generation = _nonnegative_integer(generation, "generation")
        stamp = _finite_nonnegative(stamp, "stamp")
        with self._lock:
            if pts in self._entries:
                entry = self._entries[pts]
                if entry is not None and (
                    entry[0] is not clock_obj or entry[1] != generation
                ):
                    self._entries[pts] = None
                return
            if len(self._entries) == self._max_entries:
                self._entries.popitem(last=False)
            self._entries[pts] = (clock_obj, generation, stamp)

    def consume(
        self, pts: int, current_clock: object, current_generation: int
    ) -> float:
        """Return a matching, unambiguous saved UTC exactly once, else reject."""
        pts = _valid_pts(pts)
        current_generation = _nonnegative_integer(current_generation, "current_generation")
        with self._lock:
            try:
                entry = self._entries[pts]
            except KeyError:
                raise LookupError(f"no output dispatch timestamp for PTS {pts}") from None
            if entry is None:
                raise LookupError(f"ambiguous output dispatch for PTS {pts}")
            clock_obj, generation, stamp = entry
            if clock_obj is not current_clock or generation != current_generation:
                self._entries[pts] = None
                raise LookupError(f"stale output dispatch for PTS {pts}")
            if stamp is None:
                raise LookupError(f"no unconsumed output dispatch timestamp for PTS {pts}")
            self._entries[pts] = (clock_obj, generation, None)
            return stamp
