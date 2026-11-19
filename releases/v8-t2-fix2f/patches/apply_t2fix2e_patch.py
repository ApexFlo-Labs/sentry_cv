#!/usr/bin/env python3
"""T2-FIX2e: fix of FIX2d (scene grid) lost samples. Apply on top of apply_t2fix2d_patch.py.
Measured on .64 f2c-S-8x8-grid: phases exactly even (0.04, 0.16, ... 0.91 of 2.0 s) but 52-57 samples per camera instead
of 71-72, harness coverage FAIL (needs >= 54 in the 120 s steady window; got 43).
Cause (code): scene-only cameras get pixels only when `_periodic_scene_due` says so, and that still used the old rule
(interval since the last sample), which is later than the grid due time. At the grid time the frame had no pixels,
`_scene` returned without a sample, and FIX2d had already advanced the due time, so the slot was lost.
Fix: (1) `_periodic_scene_due` uses the grid due time when a grid exists for the key; (2) the due time advances only
after a sample was taken (or when a recent event scene stands in for it); a refused frame is retried next frame.
usage: apply_t2fix2e_patch.py <dir containing v8_events.py>"""
import sys
p = sys.argv[1] + '/v8_events.py'
s = open(p).read()
old1 = """                    if now >= due:
                        while due <= now:
                            due += interval
                        if now - self.last_scene.get(key, 0) >= interval / 2:
                            self._scene(packet, binding, zone_id, now, frame_id, "interval")
                    grid[key] = (due, interval)
"""
new1 = """                    if now >= due:
                        recent_event = now - self.last_scene.get(key, 0) < interval / 2
                        if not recent_event:
                            self._scene(packet, binding, zone_id, now, frame_id, "interval")
                        if recent_event or self.last_scene.get(key) == now:
                            while due <= now:  # T2-FIX2e: advance only after a sample (else retry next frame)
                                due += interval
                    grid[key] = (due, interval)
"""
old2 = """            interval = float(binding["sample_interval_seconds"]) * multiplier
            if any(
                now - self.last_scene.get((binding["use_case_id"], zone_id), 0)
                >= interval
                for zone_id in areas
            ):
                return True
        return False
"""
new2 = """            interval = float(binding["sample_interval_seconds"]) * multiplier
            grid = getattr(self, "_t2_scene_grid", None) or {}
            for zone_id in areas:
                key = (binding["use_case_id"], zone_id)
                due, due_interval = grid.get(key, (None, None))
                if due is not None and due_interval == interval:  # T2-FIX2e: pixels at the grid due time
                    if now >= due:
                        return True
                elif now - self.last_scene.get(key, 0) >= interval:
                    return True
        return False
"""
for a, b in ((old1, new1), (old2, new2)):
    assert s.count(a) == 1, 'anchor not found: ' + a[:60]
    s = s.replace(a, b)
open(p, 'w').write(s)
print('ok')
