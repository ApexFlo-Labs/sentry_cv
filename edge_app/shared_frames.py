"""Lock-free latest-frame slots shared by ingest and inference processes."""
from __future__ import annotations

import mmap
import os
from pathlib import Path
import re
import struct
import time

import numpy as np


HEADER = struct.Struct("<QQII")  # sequence, capture timestamp ns, width, height


def slot_path(camera_id: str, stream: str = "primary") -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(camera_id))
    safe_stream = re.sub(r"[^A-Za-z0-9_.-]", "_", str(stream))
    return Path(f"/dev/shm/sentry-cv-{os.getuid()}-{safe_stream}-{safe_id}.bgr")


class SharedFrameWriter:
    """Single-writer seqlock slot. New frames overwrite stale frames."""

    def __init__(self, camera_id: str, width: int, height: int,
                 stream: str = "primary", pixel_format: str = "BGR"):
        self.width = int(width)
        self.height = int(height)
        self.pixel_format = pixel_format.upper()
        self.shape = (
            (self.height * 3 // 2, self.width, 1)
            if self.pixel_format == "NV12"
            else (self.height, self.width, 3)
        )
        self.payload_size = int(np.prod(self.shape))
        self.path = slot_path(camera_id, stream)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            expected = HEADER.size + self.payload_size
            if os.fstat(fd).st_size != expected:
                os.ftruncate(fd, expected)
            self.mapping = mmap.mmap(fd, expected, access=mmap.ACCESS_WRITE)
        finally:
            os.close(fd)
        self.payload = np.ndarray(
            self.shape, dtype=np.uint8,
            buffer=self.mapping, offset=HEADER.size,
        )
        current = struct.unpack_from("<Q", self.mapping, 0)[0]
        self.sequence = current + (2 if current % 2 == 0 else 1)

    def write(self, frame: np.ndarray, timestamp: float | None = None) -> None:
        if frame.shape != self.payload.shape or frame.dtype != np.uint8:
            raise ValueError(
                f"unexpected frame {frame.shape}/{frame.dtype}; expected "
                f"{self.payload.shape}/uint8"
            )
        odd = self.sequence + 1
        complete = self.sequence + 2
        struct.pack_into("<Q", self.mapping, 0, odd)
        struct.pack_into(
            "<QII", self.mapping, 8,
            int((timestamp if timestamp is not None else time.time()) * 1_000_000_000),
            self.width, self.height,
        )
        np.copyto(self.payload, frame)
        struct.pack_into("<Q", self.mapping, 0, complete)
        self.sequence = complete

    def close(self) -> None:
        self.payload = None
        self.mapping.close()


class SharedFrameReader:
    """Read consistent snapshots without blocking the ingest process."""

    def __init__(self, camera_id: str, width: int, height: int,
                 stream: str = "primary", pixel_format: str = "BGR"):
        self.width = int(width)
        self.height = int(height)
        self.pixel_format = pixel_format.upper()
        self.shape = (
            (self.height * 3 // 2, self.width, 1)
            if self.pixel_format == "NV12"
            else (self.height, self.width, 3)
        )
        self.payload_size = int(np.prod(self.shape))
        self.path = slot_path(camera_id, stream)
        self.mapping: mmap.mmap | None = None
        self.payload: np.ndarray | None = None
        self.last_sequence = 0

    def _connect(self) -> bool:
        if self.mapping is not None:
            return True
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            expected = HEADER.size + self.payload_size
            if os.fstat(fd).st_size != expected:
                return False
            self.mapping = mmap.mmap(fd, expected, access=mmap.ACCESS_READ)
            self.payload = np.ndarray(
                self.shape, dtype=np.uint8,
                buffer=self.mapping, offset=HEADER.size,
            )
            return True
        finally:
            os.close(fd)

    def read_latest(self) -> tuple[int, float, np.ndarray] | None:
        if not self._connect():
            return None
        assert self.mapping is not None and self.payload is not None
        first = struct.unpack_from("<Q", self.mapping, 0)[0]
        if first == 0 or first & 1 or first == self.last_sequence:
            return None
        timestamp_ns, width, height = struct.unpack_from("<QII", self.mapping, 8)
        if width != self.width or height != self.height:
            return None
        frame = self.payload.copy()
        second = struct.unpack_from("<Q", self.mapping, 0)[0]
        if first != second or second & 1:
            return None
        self.last_sequence = second
        return second, timestamp_ns / 1_000_000_000.0, frame

    def close(self) -> None:
        self.payload = None
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
