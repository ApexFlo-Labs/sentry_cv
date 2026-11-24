#!/usr/bin/env python3
"""T2 parity (user decision 2026-11-24): functions the latest T2 image (sentry_cv v8-t2-fix2f) has and her pipeline
lacked, added WITHOUT changing how many frames each use case processes. Applied to COPIES in the build context after
apply_contract_patch.py; every replacement must match exactly once, or the build stops.

    t2_parity_patch.py <context>/home/chandana_optimization/integrate

  A. Plate rules as T2 (V8 standard): her POLICY_OURS override (1 read, OCR >= 0.40, plate >= 16x5, no sharpness floor)
     is removed, so V8's adaptive ANPR rule applies (2 equal reads, or one read >= 0.82 balanced / 0.88 strict, plate
     size and sharpness floors). OCR text is decoded as T2's decode_logits: no 0.40 blanking, no Indian-format filter
     (V8's rules decide). The C plate state confirms a vehicle only at >= 0.88 (Dockerfile PLATE_CONFIRM_CONF), so it
     never stops reading before V8 may send.
  B. Body sample rules as V8 standard (her override of the identity profile removed). Identity-frame schedule unchanged.
"""
import sys
from pathlib import Path


def patch(path, edits):
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            sys.exit(f"PATCH FAILED {path.name}: expected 1 match, found {n}:\n{old}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"patched {path} ({len(edits)} edits)")


root = Path(sys.argv[1])

# A + B: V8 standard ANPR and body rules (her POLICY_OURS keeps only its identity-frame schedule)
patch(root / "event_worker_shm.py", [
    ("        from pipeline import adaptive_policy as _ap\n"
     "        for _mode, _secs in _ap.AdaptivePolicyResolver._PROFILES.items():\n"
     "            _secs[\"anpr\"].update(confirm_min_reads=1, ocr_emit_min_confidence=0.40, min_plate_width_px=16.0,\n"
     "                                 min_plate_height_px=5.0, min_sharpness=0.0)\n"
     "            _secs[\"identity\"].update(body_min_width_px=32.0, body_min_height_px=64.0, body_quality_floor=0.2)\n"
     "        print(f\"{time.strftime('%T')} {name}: POLICY ours: plates 1 read, OCR >= 0.40, plate >= 16x5 px, no sharpness floor; \"\n"
     "              f\"body >= 32x64 px quality >= 0.2; identity frames 0.7/s: body every 3rd, gait every one, scene every 7th\", flush=True)\n",
     "        # T2 parity A/B: plate and body rules are V8's adaptive profiles (no override); only the schedule is hers\n"
     "        print(f\"{time.strftime('%T')} {name}: POLICY: plates and body samples by V8's rules (as T2); \"\n"
     "              f\"identity frames 0.7/s: body every 3rd, gait every one, scene every 7th\", flush=True)\n"),
    ("        text = text if any(r.fullmatch(text) for r in OCR_PATTERNS) else \"\"\n",
     "        # T2 parity A: as T2's decode_logits - every decoded text is kept; V8's rules decide what is sent\n"),
    ("            if ocr_conf < 0.4:\n"
     "                text = \"\"\n",
     ""),
])

# C: gait memory cleanup as T2 (v8_events.py of v8-t2-fix2f: _remember_gait_center / _retire_gait_state and their
# call sites). A presence that ends, a removed gait profile or a session reset drops that person's silhouettes, cached
# vector and track centre; before, they were kept for the life of the camera process. Her schedule sets v8.gait only on
# identity frames (0.7/s), so cleanup uses the persistent gait object (_gait_store) on every frame.
patch(root / "v8code" / "pipeline" / "v8_events.py", [
    ("        if int(updated.get(\"config_revision\", 0)) <= int(self.config[\"config_revision\"]):\n"
     "            return\n"
     "        now = time.time()\n",
     "        if int(updated.get(\"config_revision\", 0)) <= int(self.config[\"config_revision\"]):\n"
     "            return\n"
     "        gait_was_enabled = any(\n"
     "            b[\"app\"] == \"person_identity\" and \"gaitbase-v18.1\" in b.get(\"embedding_profile_ids\", [])\n"
     "            for b in self.bindings)\n"
     "        gait_stays_enabled = any(\n"
     "            b[\"app\"] == \"person_identity\" and \"gaitbase-v18.1\" in b.get(\"embedding_profile_ids\", [])\n"
     "            for b in updated[\"bindings\"])\n"
     "        if gait_was_enabled and not gait_stays_enabled:\n"
     "            self._retire_gait_state()\n"
     "        now = time.time()\n"),
    ("            self.tracks.clear()\n"
     "            self.presences.clear()\n"
     "            self.counts.clear()\n",
     "            self.tracks.clear()\n"
     "            self.presences.clear()\n"
     "            self._retire_gait_state()\n"
     "            self.counts.clear()\n"),
    ("        if self.gait is not None and \"gaitbase-v18.1\" in profiles and packet.index % 3 == 0:\n",
     "        if self.gait is not None and \"gaitbase-v18.1\" in profiles and packet.index % 3 == 0:\n"
     "            self._remember_gait_center(state)\n"),
    ("    def _lost_and_unknown(self, active_keys, now, tracker_keys=None):\n",
     "    def _remember_gait_center(self, state):\n"
     "        key = (self.camera_id, int(state[\"track_id\"]))\n"
     "        state.setdefault(\"_gait_center_keys\", set()).add(key)\n"
     "        if not hasattr(self, \"_gait_center_owners\"):\n"
     "            self._gait_center_owners = {}\n"
     "        self._gait_center_owners[key] = state[\"id\"]\n"
     "\n"
     "    def _retire_gait_state(self, presence_id=None):\n"
     "        gait = getattr(self, \"gait\", None) or getattr(self, \"_gait_store\", None)\n"
     "        if gait is None:\n"
     "            return\n"
     "        pending = getattr(self, \"_pending_gait\", None)\n"
     "        if pending is not None and (presence_id is None or pending[3][\"id\"] == presence_id):\n"
     "            self._finish_gait(emit=False)\n"
     "        owners = getattr(self, \"_gait_center_owners\", {})\n"
     "        for name in (\"buffers\", \"buffer_times\", \"cache\"):\n"
     "            values = getattr(gait, name, None)\n"
     "            if values is not None:\n"
     "                values.clear() if presence_id is None else values.pop(presence_id, None)\n"
     "        centers = getattr(gait, \"centers\", {})\n"
     "        if presence_id is None:\n"
     "            centers.clear()\n"
     "            owners.clear()\n"
     "            for state in self.presences.values():\n"
     "                state.pop(\"_gait_center_keys\", None)\n"
     "        else:\n"
     "            state = self.presences.get(presence_id)\n"
     "            keys = (state.pop(\"_gait_center_keys\", set()) if state is not None else\n"
     "                    [key for key, owner in owners.items() if owner == presence_id])\n"
     "            for key in keys:\n"
     "                if owners.get(key) == presence_id:  # an ID reused by a different live presence belongs to it now\n"
     "                    owners.pop(key, None)\n"
     "                    centers.pop(key, None)\n"
     "\n"
     "    def _lost_and_unknown(self, active_keys, now, tracker_keys=None):\n"),
    ("                if gap >= 8:\n"
     "                    self.tracks.pop(key, None)\n"
     "                    self.presences.pop(state[\"id\"], None)\n",
     "                if gap >= 8:\n"
     "                    self._retire_gait_state(state[\"id\"])\n"
     "                    self.tracks.pop(key, None)\n"
     "                    self.presences.pop(state[\"id\"], None)\n"),
    ("                                              zone_episodes=self._episodes(state)))\n"
     "                self.tracks.pop(key, None)\n"
     "                self.presences.pop(state[\"id\"], None)\n"
     "\n"
     "    def _plate_idle_timeouts(self, now):\n",
     "                                              zone_episodes=self._episodes(state)))\n"
     "                self._retire_gait_state(state[\"id\"])\n"
     "                self.tracks.pop(key, None)\n"
     "                self.presences.pop(state[\"id\"], None)\n"
     "\n"
     "    def _plate_idle_timeouts(self, now):\n"),
])
patch(root / "event_worker_shm.py", [
    ("    id_models = (v8.body, v8.gait, v8.scene)\n",
     "    id_models = (v8.body, v8.gait, v8.scene)\n"
     "    v8._gait_store = v8.gait  # T2 parity C: gait cleanup also on frames where her schedule sets v8.gait = None\n"),
])

# D: motion gate as T2 (contract_image/motion_gate.py): gvamotiondetect + T2's SurfaceSwitch before the shared detector
# of each camera branch; quiet frames are dropped there and reported to the camera's rules (process_quiet); the stall
# check counts quiet frames as frames that came out (a still scene is not a stall).
patch(root / "integrate9.py", [
    ("import contract_glue  # noqa: E402  (V8 supervisor interface: ready/stop files, child processes)\n",
     "import contract_glue  # noqa: E402  (V8 supervisor interface: ready/stop files, child processes)\n"
     "import motion_gate  # noqa: E402  (T2 parity D: motion gate before the shared detector)\n"),
    ("        parts.append(f\"queue name=min{n} leaky=downstream max-size-buffers=4 ! gvadetect name=vdet{n} ",
     "        parts.append(f\"queue name=min{n} leaky=downstream max-size-buffers=4 ! {motion_gate.element(n)}gvadetect name=vdet{n} "),
    ("    status, stalled = [0], [0]\n",
     "    status, stalled = [0], [0]\n"
     "    quiet_last = [0]  # T2 parity D: motion-gate quiet frames counted at the last report\n"),
    ("        cg.cgates_attach_dedup_real(src(\"vd\"))  # really removes duplicates (rebuilds the analytics meta)\n",
     "        cg.cgates_attach_dedup_real(src(\"vd\"))  # really removes duplicates (rebuilds the analytics meta)\n"
     "        motion_gate.attach(pipeline, c, lambda cam, msg: send(cam, msg))  # T2 parity D\n"),
    ("            raise RuntimeError(\"head-pose step not attached\")\n",
     "            raise RuntimeError(\"head-pose step not attached\")\n"
     "        motion_gate.attach(pipeline, c, send)  # T2 parity D: same gate on a branch built live\n"),
    ("        stalled[0] = stalled[0] + 1 if total_in > 0 and main_rate < 0.05 * sum(",
     "        quiet_now = motion_gate.quiet_total(cams)\n"
     "        quiet_rate = max(0.0, (quiet_now - quiet_last[0]) / REPORT)\n"
     "        quiet_last[0] = quiet_now\n"
     "        if motion_gate.ENABLED:\n"
     "            log(f\"motion gate: quiet {quiet_rate:.1f}/s (dropped before the detector), cameras active \"\n"
     "                f\"{sum(1 for c in cams if c.get('motion') is not None and c['motion'].switch.tracking)}/{len(cams)} | \"\n"
     "                + motion_gate.summary(cams))\n"
     "        stalled[0] = stalled[0] + 1 if total_in > 0 and main_rate + quiet_rate < 0.05 * sum("),
])

# E: event time = when the frame arrived (as T2's frame_time: source PTS anchored to the receive clock), not when the
# engine finished it. Pipeline clock = CLOCK_MONOTONIC; base time is in the shared header ([7]); arrival =
# base + PTS, converted to wall time. Falls back to the output time if that is not available or not plausible.
# D (event side): a quiet frame from the motion gate -> V8EventPipeline.process_quiet + camera status counters.
# F: scene turn-taking as T2 fix2f: her scene rate is kept (one sample per 10 s = every 7th identity frame), but each
# camera has its own evenly spaced slot k/n on a shared wall clock (cameras sorted by id from CAMERAS_FILE, re-read
# every 30 s, so added/removed cameras re-spread); the slot advances only after a sample was taken. Her identity
# frames (1/s, 7 of every 10 s) are counted by the clock instead of by frames, so the motion gate does not change them.
patch(root / "event_worker_shm.py", [
    ("        return pts, t_real, lat, text\n",
     "        return pts, t_real, lat, text\n"
     "\n"
     "    _wall_minus_mono = time.time_ns() - time.monotonic_ns()\n"
     "\n"
     "    def arrival_s(pts, t_real):\n"
     "        \"\"\"T2 parity E: wall time the frame arrived (pipeline base + PTS on CLOCK_MONOTONIC).\"\"\"\n"
     "        base = int(H[7])\n"
     "        if base > 0 and pts >= 0:\n"
     "            t = (base + pts + _wall_minus_mono) / 1e9\n"
     "            if 0.0 <= t_real / 1e9 - t < 30.0:\n"
     "                return t\n"
     "        return t_real / 1e9\n"),
    ("                    evaluated = (frame_count - 1) % face_interval == 0\n"
     "                    observed = t_real / 1e9\n",
     "                    evaluated = (frame_count - 1) % face_interval == 0\n"
     "                    observed = arrival_s(pts, t_real)\n"),
    ("                    add(\"fire\", 1)\n"
     "                    observed = t_real / 1e9\n",
     "                    add(\"fire\", 1)\n"
     "                    observed = arrival_s(pts, t_real)\n"),
    ("            if message[0] == \"status\":\n",
     "            if message[0] == \"quiet\":  # T2 parity D: a frame the motion gate kept from the models\n"
     "                try:\n"
     "                    quiet_count[0] += 1\n"
     "                    v8.process_quiet(arrival_s(int(message[1]), time.time_ns()), frame_count)\n"
     "                    _status.quiet()\n"
     "                except Exception as exc:  # noqa: BLE001\n"
     "                    add(\"errors\", 1)\n"
     "                    print(f\"{time.strftime('%T')} {name}: quiet frame error: {type(exc).__name__}: {exc}\", flush=True)\n"
     "            if message[0] == \"status\":\n"),
    ("    scene_phase = zlib.crc32(name.encode(\"utf-8\")) % 7  # as the secondary code's scene phase per camera\n",
     "    scene_phase = zlib.crc32(name.encode(\"utf-8\")) % 7  # as the secondary code's scene phase per camera\n"
     "    quiet_count = [0]\n"
     "    id_sec = [None]  # T2 parity F: last wall-clock second that had an identity frame\n"
     "\n"
     "    def identity_due(t):\n"
     "        sec = int(t)\n"
     "        return sec != id_sec[0] and sec % 10 < 7\n"
     "\n"
     "    SCENE_PERIOD = 10.0  # her scene rate: every 7th identity frame = one sample per 10 s\n"
     "    scene_grid = {\"due\": None, \"slot\": None, \"checked\": -1e9}\n"
     "\n"
     "    def scene_slot():\n"
     "        now_m = time.monotonic()\n"
     "        if scene_grid[\"slot\"] is None or now_m - scene_grid[\"checked\"] > 30:\n"
     "            scene_grid[\"checked\"] = now_m\n"
     "            try:\n"
     "                ids = sorted(str(c[\"camera_id\"]) for c in json.load(open(os.environ[\"CAMERAS_FILE\"]))[\"cameras\"])\n"
     "                slot = ids.index(name) / len(ids)\n"
     "            except (OSError, ValueError, KeyError):\n"
     "                slot = (zlib.crc32(name.encode(\"utf-8\")) % 1000) / 1000.0\n"
     "            if slot != scene_grid[\"slot\"]:\n"
     "                scene_grid[\"slot\"], scene_grid[\"due\"] = slot, None\n"
     "                print(f\"{time.strftime('%T')} {name}: scene slot {slot:.3f} of every {SCENE_PERIOD:g} s\", flush=True)\n"
     "        return scene_grid[\"slot\"]\n"
     "\n"
     "    def scene_due(now):\n"
     "        slot = scene_slot()\n"
     "        if scene_grid[\"due\"] is None:  # as T2 fix2d: next grid time (m + slot) * period\n"
     "            due = (int(now / SCENE_PERIOD - slot) + 1 + slot) * SCENE_PERIOD\n"
     "            while due - SCENE_PERIOD >= now:\n"
     "                due -= SCENE_PERIOD\n"
     "            scene_grid[\"due\"] = due\n"
     "        return now >= scene_grid[\"due\"]\n"
     "\n"
     "    def scene_taken(now):  # as T2 fix2e: advance only after a sample\n"
     "        while scene_grid[\"due\"] is not None and scene_grid[\"due\"] <= now:\n"
     "            scene_grid[\"due\"] += SCENE_PERIOD\n"),
    ("                                  or (frame_count % fps == 0 and (frame_count // fps) % 10 < 7))  # identity frame (0.7/s)\n",
     "                                  or identity_due(observed) or scene_due(observed))  # identity frame (0.7/s, by the clock) or scene slot: T2 parity F\n"),
    ("                            second = frame_count // fps\n"
     "                            identity_frame = (frame_count % fps == 0) and (second % 10 < 7)\n",
     "                            identity_frame = identity_due(observed)  # T2 parity F: 1/s by the clock, 7 of every 10 s\n"
     "                            if identity_frame:\n"
     "                                id_sec[0] = int(observed)\n"),
    ("                                v8.scene = id_models[2] if (k + scene_phase) % 7 == 0 else None\n",
     "                                v8.scene = None  # T2 parity F: set below, on this camera's own slot\n"),
    ("                            add(\"identity_frames\", int(identity_frame))\n",
     "                            add(\"identity_frames\", int(identity_frame))\n"
     "                            if ours_policy:  # T2 parity F: scene on its own slot, checked on every frame\n"
     "                                v8.scene = id_models[2] if scene_due(observed) else None\n"),
    ("                        v8.process(packet, faces)\n",
     "                        scene_before = dict(v8.last_scene)\n"
     "                        v8.process(packet, faces)\n"
     "                        if v8.scene is not None and v8.last_scene != scene_before:\n"
     "                            scene_taken(observed)\n"),
])
