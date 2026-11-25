#!/usr/bin/env python3
"""Tracking test switches (user 2026-11-25: compare trackers and the body reconnect). Defaults keep the pipeline as is.
  REID_INTERVAL  deep-sort only: TransReID every Nth frame (default 1)
  TRACKER_TYPE   gvatrack tracking-type (default zero-term-imageless, hers; T2 uses zero-term). deep-sort adds a
                 TransReID vector per person per frame (gvainference, GPU) in front of the tracker.
  REID_ON_NEW=1  V8 reconnects a lost person by body vector only when one exists on the frame a new track appears
                 (T2 computes body vectors on every frame); her schedule has one ~every 4 s, so reconnects never happen.
                 With this switch, on a frame where a new person track appears AND a lost person with a body vector
                 exists, a body vector is computed for the new track(s) only.
  FACE_RECONNECT=1  (user 2026-11-25: use face to keep a person's track) each person keeps its 5 best face vectors
                 (AdaFace, quality >= V8's face_min_quality); a new, not yet confirmed person track that shows a face is
                 matched against MISSING or LOST people nearby (cosine >= FACE_MATCH 0.45, 0.05 ahead of the next) and
                 continues that presence (presence_recovered if it was already lost; silent continuation if only missing,
                 as if the tracker had kept the ID). While such a person is missing nearby, a new track waits up to
                 FACE_HOLD_S (1 s, faces come every 5th frame) before it is confirmed; all other tracks confirm as before.
Applied after anpr_patch.py; every replacement must match exactly once, or the build stops.

    tracking_patch.py <context>/home/chandana_optimization/integrate
"""
import json
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
# TransReID as a gvainference model for deep-sort: RGB 256x128, -1..1 (as person_reid.py "rgb256x128-minus1to1-v1")
(root / "reid_model_proc.json").write_text(json.dumps({
    "json_schema_version": "2.2.0",
    "input_preproc": [{"format": "image",
                       "params": {"color_space": "RGB", "resize": "no-aspect-ratio", "range": [-1.0, 1.0]}}],
    "output_postproc": [{"converter": "raw_data_copy", "attribute_name": "reid", "layer_name": "feat"}]}, indent=1))

patch(root / "integrate9.py", [
    ('PLATE_INLINE = os.getenv("PLATE_INLINE") == "1"\n',
     'PLATE_INLINE = os.getenv("PLATE_INLINE") == "1"\n'
     'TRACKER_TYPE = os.getenv("TRACKER_TYPE", "zero-term-imageless")  # tracking_patch.py (test switch)\n'),
    ('f"gvatrack tracking-type=zero-term-imageless ! {Q} ! ',
     'f"{TRACKER_ELEMS} ! {Q} ! '),
    ("    def branch_parts(n, parts):",
     "    TRACKER_ELEMS = (f\"gvainference model={SEC}/surveillance/transreid_ssl_int8.xml model-proc={HERE}/reid_model_proc.json \"\n"
     "                     f\"device=GPU inference-region=roi-list object-class=person model-instance-id=reid-shared nireq=4 \"\n"
     "                     f\"inference-interval={os.getenv('REID_INTERVAL', '1')} ! {Q} ! \"  # REID_INTERVAL=5: ReID every 5th frame (as faces)\n"
     "                     f\"gvatrack tracking-type=deep-sort deepsort-trck-cfg=max_age=60,max_cosine_distance=0.3,object_class=person,reid_max_age=30\"\n"
     "                     if TRACKER_TYPE == \"deep-sort\" else f\"gvatrack tracking-type={TRACKER_TYPE}\")  # tracking_patch.py\n"
     "    log(f\"tracker: {TRACKER_TYPE}\")\n"
     "    def branch_parts(n, parts):"),
])

patch(root / "event_worker_shm.py", [
    # deep-sort gives a person a track ID only after n_init frames (tentative tracks have none): skip those boxes
    # instead of failing the frame (the zero-term trackers always attach one, so this changes nothing for them)
    ('            if track_id is None:\n'
     '                raise RuntimeError("gvatrack did not attach a track ID")\n',
     '            if track_id is None:\n'
     '                if TRACKER_TENTATIVE_OK:  # tracking_patch.py: deep-sort tentative track\n'
     '                    continue\n'
     '                raise RuntimeError("gvatrack did not attach a track ID")\n'),
    ('import collections\n',
     'import collections\n'
     'TRACKER_TENTATIVE_OK = os.getenv("TRACKER_TYPE") == "deep-sort"  # tracking_patch.py\n'),
    ("                        v8.process(packet, faces)\n",
     "                        reid_now = None\n"
     "                        if reid_on_new and v8.body is None and id_models[0] is not None:  # tracking_patch.py REID_ON_NEW\n"
     "                            if any(st[\"kind\"] == \"person\" and st[\"lost\"] and st.get(\"body_vector\") is not None for st in v8.presences.values()):\n"
     "                                reid_now = {str(d.metadata[\"track_id\"]) for d in packet.detections\n"
     "                                            if d.class_name == \"pedestrian\" and d.metadata.get(\"track_id\") is not None\n"
     "                                            and (\"person\", str(d.metadata[\"track_id\"])) not in v8.tracks}\n"
     "                                if reid_now:\n"
     "                                    v8.body, v8._reid_only = id_models[0], reid_now\n"
     "                                    reid_count[0] += len(reid_now)\n"
     "                        v8.process(packet, faces)\n"
     "                        if reid_now:\n"
     "                            v8.body, v8._reid_only = None, None\n"),
    ("    id_count = [0]\n",
     "    id_count = [0]\n"
     "    reid_on_new = os.getenv(\"REID_ON_NEW\") == \"1\"  # tracking_patch.py\n"
     "    reid_count = [0]  # body vectors computed for new tracks (tracking_patch.py)\n"
     "    if reid_on_new:\n"
     "        from pipeline import v8_events as _ve_r\n"
     "        _orig_bv = _ve_r.V8EventPipeline._body_vectors\n"
     "\n"
     "        def _bv_only(self, packet, detections, now):  # body vectors only for the new tracks on a reconnect frame\n"
     "            only = getattr(self, \"_reid_only\", None)\n"
     "            if only:\n"
     "                detections = [d for d in detections if str(d.metadata.get(\"track_id\")) in only]\n"
     "            return _orig_bv(self, packet, detections, now)\n"
     "        _ve_r.V8EventPipeline._body_vectors = _bv_only\n"
     "        print(f\"{time.strftime('%T')} {name}: body vector for new tracks when a lost person can be reconnected\", flush=True)\n"),
])

V8 = root / "v8code" / "pipeline" / "v8_events.py"
patch(V8, [
    ("import uuid\n",
     "import uuid\n"
     "FACE_RECONNECT = os.getenv(\"FACE_RECONNECT\") == \"1\"  # tracking_patch.py\n"
     "FACE_MATCH = float(os.getenv(\"FACE_MATCH\", \"0.45\"))\n"
     "FACE_HOLD_S = float(os.getenv(\"FACE_HOLD_S\", \"1.0\"))\n"),
    # helpers next to V8's own reassociation
    ("    def _reassociate(self, kind, box, vector, now, active_keys, class_name=None):\n",
     "    def _missing_people(self, state, box, now, active_keys):  # tracking_patch.py FACE_RECONNECT\n"
     "        out = []\n"
     "        for st in self.presences.values():\n"
     "            if st is state or st[\"kind\"] != \"person\" or not st.get(\"confirmed\") or st[\"track_key\"] in active_keys:\n"
     "                continue\n"
     "            if not st.get(\"face_mem\"):\n"
     "                continue\n"
     "            _, ended_after = self._presence_timeouts(st)\n"
     "            if now - st[\"last_seen\"] > ended_after:\n"
     "                continue\n"
     "            if np.linalg.norm(np.subtract(self._center(box), self._center(st[\"box\"]))) > self._recovery_distance_limit(st, box):\n"
     "                continue\n"
     "            out.append(st)\n"
     "        return out\n"
     "\n"
     "    def _face_reassociate(self, state, box, face, now, active_keys):  # tracking_patch.py FACE_RECONNECT\n"
     "        if face is None or getattr(face, \"embedding\", None) is None:\n"
     "            return None\n"
     "        if float(face.quality) < float(self.policy.section(\"identity\")[\"face_min_quality\"]):\n"
     "            return None\n"
     "        v = np.asarray(face.embedding, np.float32).reshape(-1)\n"
     "        v = v / (np.linalg.norm(v) + 1e-9)\n"
     "        scored = sorted(((max(float(np.dot(v, m)) for _, m in st[\"face_mem\"]), st)\n"
     "                         for st in self._missing_people(state, box, now, active_keys)), key=lambda x: -x[0])\n"
     "        if not scored or scored[0][0] < FACE_MATCH:\n"
     "            return None\n"
     "        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:\n"
     "            return None\n"
     "        if self.telemetry is not None:\n"
     "            self.telemetry.count(\"face_reconnect\")\n"
     "        return scored[0][1]\n"
     "\n"
     "    def _reassociate(self, kind, box, vector, now, active_keys, class_name=None):\n"),
    # a brand-new person presence is marked, so only those can be joined to a missing person by face
    ("                    self.presences[state[\"id\"]] = state\n",
     "                    self.presences[state[\"id\"]] = state\n"
     "                    state[\"_new\"] = kind == \"person\"  # tracking_patch.py FACE_RECONNECT\n"),
    # face match while the new track is not confirmed yet: it continues the missing/lost presence
    ("            previous_last_seen = state[\"last_seen\"]\n",
     "            if FACE_RECONNECT and state.get(\"_new\") and not state.get(\"confirmed\"):  # tracking_patch.py\n"
     "                match = self._face_reassociate(state, item.bbox, face_by_track.get(track_id), now, active_keys)\n"
     "                if match is not None:\n"
     "                    self.presences.pop(state[\"id\"], None)\n"
     "                    self.tracks.pop(match[\"track_key\"], None)\n"
     "                    previous = match[\"track_id\"]\n"
     "                    match[\"track_key\"], match[\"track_id\"] = key, track_id\n"
     "                    self.tracks[key] = match\n"
     "                    recovered_from = previous if match[\"lost\"] else None  # only missing: silent continuation\n"
     "                    state = match\n"
     "            previous_last_seen = state[\"last_seen\"]\n"),
    # face memory: the 5 best faces of each person
    ("            if vector is not None:\n"
     "                state[\"body_vector\"] = vector\n"
     "                state[\"last_body_at\"] = now\n",
     "            if vector is not None:\n"
     "                state[\"body_vector\"] = vector\n"
     "                state[\"last_body_at\"] = now\n"
     "            if FACE_RECONNECT and kind == \"person\":  # tracking_patch.py: face memory\n"
     "                _f = face_by_track.get(track_id)\n"
     "                if (_f is not None and getattr(_f, \"embedding\", None) is not None\n"
     "                        and float(_f.quality) >= float(self.policy.section(\"identity\")[\"face_min_quality\"])):\n"
     "                    _v = np.asarray(_f.embedding, np.float32).reshape(-1)\n"
     "                    _mem = state.setdefault(\"face_mem\", [])\n"
     "                    _mem.append((float(_f.quality), _v / (np.linalg.norm(_v) + 1e-9)))\n"
     "                    _mem.sort(key=lambda x: -x[0])\n"
     "                    del _mem[5:]\n"),
    # short wait before confirming a new track while a missing person with a face memory is nearby
    ("            if state[\"hits\"] < stable_hits:\n"
     "                continue\n",
     "            if state[\"hits\"] < stable_hits:\n"
     "                continue\n"
     "            if (FACE_RECONNECT and state.get(\"_new\") and not state.get(\"confirmed\")\n"
     "                    and now - state[\"first_seen\"] < FACE_HOLD_S\n"
     "                    and self._missing_people(state, item.bbox, now, active_keys)):  # tracking_patch.py\n"
     "                continue\n"),
])
