#!/usr/bin/env python3
"""Tracking test switches (user 2026-11-25: compare trackers and the body reconnect). Defaults keep the pipeline as is.
  TRACKER_TYPE   gvatrack tracking-type (default zero-term-imageless, hers; T2 uses zero-term). deep-sort adds a
                 TransReID vector per person per frame (gvainference, GPU) in front of the tracker.
  REID_ON_NEW=1  V8 reconnects a lost person by body vector only when one exists on the frame a new track appears
                 (T2 computes body vectors on every frame); her schedule has one ~every 4 s, so reconnects never happen.
                 With this switch, on a frame where a new person track appears AND a lost person with a body vector
                 exists, a body vector is computed for the new track(s) only.
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
     "                     f\"device=GPU inference-region=roi-list object-class=person model-instance-id=reid-shared nireq=4 ! {Q} ! \"\n"
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
