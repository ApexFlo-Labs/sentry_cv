#!/usr/bin/env python3
"""Read bounded native API progress records; no calls into the GPU driver."""
import argparse
from datetime import datetime, timezone
import json
import mmap
from pathlib import Path
import struct
import time

HEADER = struct.Struct('<8s7Q')
SLOT = struct.Struct('<10Qq80s')


def snapshot(path, now_ns=None):
    supplied_now = now_ns
    with Path(path).open('rb') as file:
        if file.seek(0, 2) < HEADER.size:
            raise ValueError('incomplete header')
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            magic, abi, pid, count, size, offset, started, _ = HEADER.unpack_from(data)
            if (magic != b'SCLACT1\0' or abi != 1 or count > 256
                    or size < SLOT.size or size > 1024 or offset < HEADER.size
                    or offset + count * size > len(data)):
                raise ValueError('unsupported native progress layout')
            rows = []
            unstable = 0
            for index in range(count):
                at = offset + index * size
                before = struct.unpack_from('<Q', data, at)[0]
                if before & 1:
                    unstable += 1
                    continue
                values = SLOT.unpack_from(data, at)
                after = struct.unpack_from('<Q', data, at)[0]
                if before != after or after & 1:
                    unstable += 1
                    continue
                _, tid, calls, completed, began, ended, active, kernel, wall, maximum, status, name = values
                if not tid:
                    continue
                # Take the clock after the coherent slot copy: a fresh API can
                # start while the reader walks earlier slots.
                row_now = time.monotonic_ns() if supplied_now is None else supplied_now
                rows.append(dict(tid=tid, calls=calls, completed=completed,
                    active=bool(active), api=name.split(b'\0', 1)[0].decode(errors='replace'),
                    active_age_seconds=(row_now-began)/1e9 if active else None,
                    last_completed_age_seconds=(row_now-ended)/1e9 if ended else None,
                    kernel_handle=hex(kernel), summed_host_wall_ns=wall,
                    max_host_wall_ns=maximum, last_status=status))
            return dict(path=str(path), pid=pid, process_epoch_monotonic_ns=started,
                monotonic_ns=time.monotonic_ns() if supplied_now is None else supplied_now,
                utc=datetime.now(timezone.utc).isoformat(),
                unstable_slots=unstable, slots=rows,
                scope='Diagnostic host API progress; not kernel duration or GPU utilization')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=float, default=900)
    a = p.parse_args()
    stop = time.monotonic() + a.seconds
    with a.output.open('x') as output:
        while time.monotonic() < stop:
            for path in sorted(a.root.glob('sentry-v8-capacity-*/delivery/ocl-active-*.bin')):
                try:
                    record = snapshot(path)
                except (OSError, ValueError) as exc:
                    record = dict(path=str(path), error=str(exc))
                output.write(json.dumps(record) + '\n')
            output.flush()
            time.sleep(1)


if __name__ == '__main__':
    main()
