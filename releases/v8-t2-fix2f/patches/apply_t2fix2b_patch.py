"""T2-FIX2b: even scene slots. With SENTINEL_SCENE_SPREAD=1 the first scene sample of camera k of n in a worker group is
offset by k/n of the interval (was crc32(camera name) -> collisions). Camera not in the map -> old crc32 phase."""
import sys
d = sys.argv[1]
def patch(p, old, new):
    s = open(p).read(); assert s.count(old) == 1, (p, old[:60]); open(p, 'w').write(s.replace(old, new))
patch(d + '/stream_fleet_openvino.py',
      "    os.environ[\"SENTINEL_DLSTREAMER_CAMERA_MODEL_SHARDS\"] = json.dumps(\n",
      "    # T2-FIX2b: even scene slots inside this worker group (camera order).\n"
      "    os.environ[\"SENTINEL_SCENE_SLOTS\"] = json.dumps(\n"
      "        {camera[\"name\"]: index / max(1, len(cameras)) for index, camera in enumerate(cameras)},\n"
      "        separators=(\",\", \":\"), sort_keys=True)\n"
      "    os.environ[\"SENTINEL_DLSTREAMER_CAMERA_MODEL_SHARDS\"] = json.dumps(\n")
patch(d + '/v8_events.py',
      "                    phase = (zlib.crc32(str(self.camera_id).encode()) % 1000) / 1000.0 * interval\n",
      "                    phase = (zlib.crc32(str(self.camera_id).encode()) % 1000) / 1000.0 * interval\n"
      "                    try:  # T2-FIX2b: even slot by camera order in the worker group, if known\n"
      "                        slot = json.loads(os.getenv(\"SENTINEL_SCENE_SLOTS\", \"{}\")).get(str(self.camera_id))\n"
      "                        if slot is not None:\n"
      "                            phase = float(slot) * interval\n"
      "                    except (TypeError, ValueError):\n"
      "                        pass\n")
print("ok")
