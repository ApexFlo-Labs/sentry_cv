#!/usr/bin/env python3
"""T2-FIX2d: scene slots on a shared wall-clock grid (fix of FIX2b).
FIX2b set each camera's FIRST sample at slot*interval after that camera's own start, and every later sample was timed
from the actual sample frame, so (a) cameras starting at different times did not share a reference and (b) frame
jitter (up to 1/fps) accumulated. Measured on .64 f2-S-8x8-slots: phases 0.07..0.88, closest pair 2.3 % (ideal 12.5 %).
Now (only with SENTINEL_SCENE_SPREAD=1 and a slot from SENTINEL_SCENE_SLOTS): interval samples fall on the grid
t = (m + slot) * interval of frame time (wall clock, shared by all cameras); the next due time advances by whole
intervals. An event scene taken less than half an interval before a grid time replaces that grid sample (as before,
event scenes reset the interval). Without a slot the old behaviour is unchanged.
usage: apply_t2fix2d_patch.py <dir containing v8_events.py>"""
import sys
d = sys.argv[1]
p = d + '/v8_events.py'
s = open(p).read()
old = """                if key not in self.last_scene and os.getenv("SENTINEL_SCENE_SPREAD", "0") == "1":
                    import zlib
                    phase = (zlib.crc32(str(self.camera_id).encode()) % 1000) / 1000.0 * interval
                    try:  # T2-FIX2b: even slot by camera order in the worker group, if known
                        slot = json.loads(os.getenv("SENTINEL_SCENE_SLOTS", "{}")).get(str(self.camera_id))
                        if slot is not None:
                            phase = float(slot) * interval
                    except (TypeError, ValueError):
                        pass
                    self.last_scene[key] = now - interval + phase
                if now - self.last_scene.get(key, 0) >= interval:
                    self._scene(packet, binding, zone_id, now, frame_id, "interval")
"""
new = """                slot = None
                if os.getenv("SENTINEL_SCENE_SPREAD", "0") == "1":
                    try:  # T2-FIX2b/2d: even slot by camera order in the worker group, if known
                        slot = json.loads(os.getenv("SENTINEL_SCENE_SLOTS", "{}")).get(str(self.camera_id))
                        slot = None if slot is None else float(slot) % 1.0
                    except (TypeError, ValueError):
                        slot = None
                if slot is not None and interval > 0:
                    # T2-FIX2d: shared wall-clock grid t = (m + slot) * interval; advance by whole intervals.
                    grid = getattr(self, "_t2_scene_grid", None)
                    if grid is None:
                        grid = self._t2_scene_grid = {}
                    due, due_interval = grid.get(key, (None, None))
                    if due is None or due_interval != interval:
                        due = (int(now / interval - slot) + 1 + slot) * interval
                        while due - interval >= now:
                            due -= interval
                    if now >= due:
                        while due <= now:
                            due += interval
                        if now - self.last_scene.get(key, 0) >= interval / 2:
                            self._scene(packet, binding, zone_id, now, frame_id, "interval")
                    grid[key] = (due, interval)
                    continue
                if key not in self.last_scene and os.getenv("SENTINEL_SCENE_SPREAD", "0") == "1":
                    import zlib
                    phase = (zlib.crc32(str(self.camera_id).encode()) % 1000) / 1000.0 * interval
                    self.last_scene[key] = now - interval + phase
                if now - self.last_scene.get(key, 0) >= interval:
                    self._scene(packet, binding, zone_id, now, frame_id, "interval")
"""
assert s.count(old) == 1, 'anchor not found'
open(p, 'w').write(s.replace(old, new))
print('ok')
