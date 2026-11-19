"""Bounded test-source pacing from decode timestamps, without catch-up bursts."""
import time


def timestamp90k(value):
    return (((value[0] >> 1) & 7) << 30) | (value[1] << 22) | ((value[2] >> 1) << 15) | (value[3] << 7) | (value[4] >> 1)


def video_timestamps(packet):
    if len(packet) != 188 or packet[0] != 0x47:
        raise ValueError('unaligned MPEG-TS source')
    if not packet[1] & 0x40:
        return None
    control = (packet[3] >> 4) & 3
    if control not in (1, 3):
        return None
    offset = 4 + (1 + packet[4] if control == 3 else 0)
    payload = packet[offset:]
    if len(payload) < 14 or payload[:3] != b'\x00\x00\x01' or not 0xe0 <= payload[3] <= 0xef:
        return None
    flags = payload[7] >> 6
    if flags not in (2, 3) or (flags == 3 and len(payload) < 19):
        raise ValueError('video PES requires complete PTS/DTS for pacing')
    pts = timestamp90k(payload[9:14])
    dts = timestamp90k(payload[14:19]) if flags == 3 else pts
    return pts, dts


def frame_groups(stream, maximum_group_bytes=8 * 1024 * 1024):
    """Preserve every TS byte; group by video PES without decoding/re-encoding."""
    carry = bytearray()
    group = bytearray()
    current = None
    while True:
        chunk = stream.read(188 * 32)
        if not chunk:
            break
        carry.extend(chunk)
        complete = len(carry) // 188 * 188
        for offset in range(0, complete, 188):
            packet = bytes(carry[offset:offset+188])
            stamp = video_timestamps(packet)
            if stamp is not None:
                if current is not None:
                    yield current, bytes(group)
                    group.clear()
                current = stamp
            group.extend(packet)
            if len(group) > maximum_group_bytes:
                raise ValueError('bounded MPEG-TS frame group exceeded')
        del carry[:complete]
    if carry:
        raise ValueError('truncated MPEG-TS packet')
    if current is not None:
        yield current, bytes(group)
    elif group:
        raise ValueError('source contained no timestamped video')


class FramePacer:
    def __init__(self, clock=time.monotonic, wait=time.sleep):
        self.clock = clock
        self.wait = wait
        self.last_timestamp = None
        self.last_sent = None

    def pace(self, dts):
        now = self.clock()
        if self.last_timestamp is not None:
            ticks = (dts - self.last_timestamp + (1 << 32)) % (1 << 33) - (1 << 32)
            if not 0 < ticks <= 5 * 90000:
                raise ValueError('unsupported discontinuous video DTS in test fixture')
            deadline = self.last_sent + ticks / 90000
            while now < deadline:
                self.wait(deadline - now)
                now = self.clock()
        # Anchor each next interval to this actual send time. A stopped process
        # emits one current frame on resume, then waits a full source interval.
        # Never compress a missed wall-clock interval into a catch-up burst.
        self.last_timestamp = dts
        self.last_sent = now
        return now
