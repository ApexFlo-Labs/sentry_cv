#!/usr/bin/env python3
"""Applies the contract glue to COPIES of Chandana's integrate9.py and event_worker_shm.py (in the build context).
Every replacement must match exactly once, or the build stops: if her code changes, nothing is applied silently.

    apply_contract_patch.py <context>/home/chandana_optimization/integrate

What changes (nothing else):
  integrate9.py
    1. cameras come from the supervisor's CAMERAS_FILE (validated desired-state revision, written by V8's adapter)
       instead of loading desired_state.json itself; same per-camera worker dicts (her code already used
       V8's _camera_to_worker on each camera).
    2. model server, handoff process and event processes start through contract_glue.child (die with the worker).
    3. watchdog: stop file from the supervisor -> stop and drain; first frame of a camera -> <camera>.ready.
  event_worker_shm.py
    4. V8's CameraTelemetry is passed to her V8EventPipeline (was telemetry=None), and V8's decoder/supervisor status
       files are written per camera (contract_glue.CameraStatus): health, readiness, fps and drops in /healthz.
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

patch(root / "integrate9.py", [
    ("import event_worker_shm as event_worker  # noqa: E402  (reads the C reader's shared memory)",
     "import event_worker_shm as event_worker  # noqa: E402  (reads the C reader's shared memory)\n"
     "import contract_glue  # noqa: E402  (V8 supervisor interface: ready/stop files, child processes)\n"
     "contract_glue.install_shared_memory(sys.argv[1])  # CONTRACT: /dev/shm or /tmp/sentinel/shm, as V8\n"
     "_DLS_ARGV = list(sys.argv)  # restored before a camera-process restart (gvapython resets sys.argv later)"),
    ("    desired = HostSecretsValidator(SECRETS).load(Path(DESIRED))\n"
     "    cams = []\n"
     "    for n, camera in enumerate(desired.cameras):\n"
     "        worker, config = camera_config(camera)\n",
     "    workers = json.loads(Path(DESIRED).read_text(encoding=\"utf-8\"))[\"cameras\"]  # CONTRACT: supervisor's validated revision\n"
     "    cams = []\n"
     "    for n, worker in enumerate(workers):\n"
     "        config = {\"camera_id\": worker[\"camera_id\"], \"name\": worker[\"name\"], \"enabled\": True,\n"
     "                  \"processing\": worker[\"processing\"], \"source\": worker[\"source\"], \"analytics\": worker[\"analytics\"]}\n"
     "        config[\"runtime_analytics\"] = normalize_runtime_analytics(config)  # as her camera_config / build_camera_config\n"),
    ("    log(f\"desired state {desired.deployment_id} revision {desired.revision}: {[(c['name'], c['fps']) for c in cams]}\")",
     "    log(f\"cameras file {DESIRED}: {[(c['name'], c['fps']) for c in cams]}\")"),
    ("    ms_proc = ctx.Process(target=model_server.serve, name=\"model-server\", daemon=True,\n"
     "                          args=(ms_paths,",
     "    ms_proc = ctx.Process(target=contract_glue.child, name=\"model-server\", daemon=True,\n"
     "                          args=(\"model_server\", \"serve\", ms_paths,"),
    ("h_proc = ctx.Process(target=handoff_host.serve, name=\"handoff\", daemon=True, args=(parent,",
     "h_proc = ctx.Process(target=contract_glue.child, name=\"handoff\", daemon=True, args=(\"handoff_host\", \"serve\", parent,"),
    ("        c[\"proc\"] = ctx.Process(target=event_worker.run, name=f\"events-{c['name']}\", daemon=True,\n"
     "                                args=(c[\"name\"],",
     "        c[\"proc\"] = ctx.Process(target=contract_glue.child, name=f\"events-{c['name']}\", daemon=True,\n"
     "                                args=(\"event_worker_shm\", \"run\", c[\"name\"],"),
    ("        c[\"proc\"].start()\n    log(f\"started {len(cams)} event processes; loading their models\")\n",
     "        c[\"proc_args\"] = c[\"proc\"]._args  # kept for a restart (Process.start() drops _args)\n"
     "        c[\"proc\"].start()\n    log(f\"started {len(cams)} event processes; loading their models\")\n"),
    ("    def watchdog():\n        now = time.time()\n",
     "    def watchdog():\n"
     "        if contract_glue.stop_requested():  # CONTRACT: supervisor asks to stop (new revision or shutdown)\n"
     "            log(\"stop requested by the supervisor\")\n"
     "            loop.quit()\n"
     "            return False\n"
     "        now = time.time()\n"),
    ("        desc = (f\"urisourcebin uri={cam['url']} ! decodebin3 ! ",
     "        _u = urlparse(cam[\"url\"])  # CONTRACT: tcp:// sources (MPEG-TS), opened as V8 does; urisourcebin has no tcp handler\n"
     "        _src = (f\"tcpclientsrc host={_u.hostname} port={_u.port} timeout=30 ! tsdemux ! decodebin3\" if _u.scheme.lower() == \"tcp\"\n"
     "                else f\"urisourcebin name=usrc uri={cam['url']} ! decodebin3\")\n"
     "        desc = (f\"{_src} ! "),
    # CONTRACT runtimeAvailability.startup / cameraIsolation: a camera that is down at start must not stop the worker.
    # (a) per-camera names on the three detectors, so an error can be traced to its camera branch
    ("! gvadetect model={os.getenv(\"VEH_MODEL\")", "! gvadetect name=vdet{n} model={os.getenv(\"VEH_MODEL\")"),
    ("{Q} ! gvadetect model=", "{Q} ! gvadetect name=sdet{n} model="),
    ("{FIRE_CONV}gvadetect model=", "{FIRE_CONV}gvadetect name=fdet{n} model="),
    # (b) unreachable at start -> not started, reported unavailable, retried by her watchdog (same path as a rejoin)
    ("    for c in cams:\n        start(c)\n    bus = pipeline.get_bus()\n",
     "    for c in cams:\n"
     "        if stream_ready(c[\"url\"]):\n"
     "            start(c)\n"
     "        else:\n"
     "            c[\"retry_at\"] = time.time() + c[\"wait\"]\n"
     "            send(c, (\"status\", \"unavailable\", \"stream_disconnected\"), droppable=False)\n"
     "            log(f\"{c['name']}: source not reachable at start; retrying\")\n"
     "    bus = pipeline.get_bus()\n"),
    # CONTRACT cameraIsolation: one camera's event process dying must not stop the other cameras -> restart it
    # (her code exits FATAL). Waits 1 s and empties that camera's model-server reply queue (stale replies), max 5 per camera.
    ("        dead = [c[\"name\"] for c in cams if not c[\"proc\"].is_alive()]\n"
     "        if dead:\n"
     "            log(f\"FATAL event process stopped: {dead}\")\n"
     "            status[0] = 5\n"
     "            loop.quit()\n",
     "        for c in [c for c in cams if not c[\"proc\"].is_alive() and not c.get(\"proc_restarting\")]:\n"
     "            c[\"restarts\"] = c.get(\"restarts\", 0) + 1\n"
     "            if c[\"restarts\"] > 5:\n"
     "                log(f\"FATAL event process of {c['name']} stopped again after 5 restarts\")\n"
     "                status[0] = 5\n"
     "                loop.quit()\n"
     "                return True\n"
     "            log(f\"{c['name']}: event process stopped (exit {c['proc'].exitcode}); restarting it ({c['restarts']}/5)\")\n"
     "            time.sleep(1)\n"
     "            while True:\n"
     "                try:\n"
     "                    ms_responses[c[\"name\"]].get_nowait()\n"
     "                except queue.Empty:\n"
     "                    break\n"
     "            old = c[\"proc\"]\n"
     "            sys.argv[:] = _DLS_ARGV  # spawn re-runs this file in the child with the parent's current sys.argv\n"
     "            c[\"proc\"] = ctx.Process(target=contract_glue.child, name=old.name, daemon=True, args=c[\"proc_args\"])\n"
     "            c[\"proc\"].start()\n"
     "            if c[\"bin\"] is not None and c[\"seen\"]:\n"
     "                send(c, (\"status\", \"healthy\", \"none\"), droppable=False)\n"),
    # Stall: her check needs exactly 0 frames out for 60 s; a GPU hang left ~0.5 of 160 fps forever. Now < 5 % of the
    # requested fps for 60 s also stops the worker, and the launcher restarts it.
    ("        stalled[0] = stalled[0] + 1 if total_in > 0 and main_rate == 0 else 0\n",
     "        stalled[0] = stalled[0] + 1 if total_in > 0 and main_rate < 0.05 * sum(c[\"fps\"] for c in cams if c[\"bin\"] is not None) else 0\n"),
    # (c) a camera branch's detector failing to stop while that camera's source is down is that camera's problem only
    ("        log(f\"FATAL from {path}: {err.message} | {debug}\")\n",
     "        import re as _re\n"
     "        _m = _re.search(r\"(vdet|sdet|fdet)(\\d+)$\", path)\n"
     "        if _m and \"failed on stop\" in str(err.message) and slots[int(_m.group(2))][\"bin\"] is None:\n"
     "            log(f\"{slots[int(_m.group(2))]['name']}: {err.message} on its idle branch (no source) - other cameras not affected\")\n"
     "            return\n"
     "        log(f\"FATAL from {path}: {err.message} | {debug}\")\n"),
    ("        cam[\"bin\"] = Gst.parse_bin_from_description(desc, False)\n",
     "        cam[\"bin\"] = Gst.parse_bin_from_description(desc, False)\n"
     "        if cam[\"bin\"].get_by_name(\"usrc\") is not None:  # video tracks only, RTSP over TCP, TLS without CA matching\n"
     "            cam[\"bin\"].get_by_name(\"usrc\").connect(\"source-setup\", contract_glue.camera_source_setup)\n"),
    ("    def stream_ready(url):\n        u = urlparse(url)\n        try:\n",
     "    def stream_ready(url):\n        u = urlparse(url)\n"
     "        if u.scheme.lower() != \"rtsp\":  # CONTRACT: tcp/http/rtsps sources (adapter allows them) - her check speaks plain RTSP\n"
     "            try:\n"
     "                socket.create_connection((u.hostname, u.port or {\"rtsps\": 322, \"https\": 443, \"http\": 80}.get(u.scheme.lower(), 554)), timeout=1).close()\n"
     "                return True\n"
     "            except OSError:\n"
     "                return False\n"
     "        try:\n"),
    ("                    cam[\"seen\"] = True\n"
     "                    send(cam, (\"status\", \"healthy\", \"none\"), droppable=False)\n",
     "                    cam[\"seen\"] = True\n"
     "                    send(cam, (\"status\", \"healthy\", \"none\"), droppable=False)\n"
     "                    contract_glue.mark_ready(cam[\"name\"])  # CONTRACT: startup admission / readiness\n"),
])

# Per-camera revision changes (user decisions 2026-11-19: a revision must only touch the cameras it changes, and adding
# or removing cameras must not disturb the other cameras; no fixed number of spare slots).
# The supervisor (dls_entrypoint.py) keeps this worker running whenever the deployment stays the same; V8's adapter
# rewrites CAMERAS_FILE and the engine applies the revision camera by camera, all changes of one revision in one pass:
#   zones / lines / bindings / scope / object types / scene interval -> that camera's event process applies them live
#     (her V8EventPipeline.reconfigure(), V8's own live path)
#   fps or source URL -> only that camera's source bin is rebuilt (her rejoin path) and its event process restarted
#   embedding profiles or apps -> only that camera's event process is restarted (it loads models by profile)
#   camera removed -> its source and event process stop, its metrics/ready files go; its branch stays idle (no source,
#     like a camera that is down) and is reused by the next camera added
#   camera added -> a free idle branch is reused, or one new branch is built into the running graph (same elements as
#     at start, same shared model instances), with its own shared memory, handoff connection and event process
#   deployment changed -> left to the supervisor (whole worker restarts)
GRAPH_EDITS = [
    ("    parts = []\n    for c in cams:\n        n = c[\"n\"]\n        parts.append(f\"queue name=min{n} ",
     "    def branch_parts(n, parts):  # CONTRACT (live add): one camera's branch; also built later for an added camera\n"
     "        parts.append(f\"queue name=min{n} "),
    ("    plate_sink = []\n    if not SEC_PROCESS:\n",
     "        return parts\n\n"
     "    parts = []\n"
     "    for c in cams:\n"
     "        branch_parts(c[\"n\"], parts)\n"
     "    plate_sink = []\n    if not SEC_PROCESS:\n"),
    ("    log(f\"cameras file {DESIRED}: {[(c['name'], c['fps']) for c in cams]}\")\n",
     "    log(f\"cameras file {DESIRED}: {[(c['name'], c['fps']) for c in cams]}\")\n"
     "    slots = list(cams)  # every branch ever built, index = branch number n; cams = the cameras of the current revision\n"),
    ("        send(cams[int(key)], (\"plates\", items))\n",
     "        if slots[int(key)].get(\"active\", True):\n"
     "            send(slots[int(key)], (\"plates\", items))\n"),
    ("    os.environ[\"SENTINEL_CV_HANDOFF_STATUS_PATH\"] = STATUS_PATH\n",
     "    os.environ[\"SENTINEL_CV_HANDOFF_STATUS_PATH\"] = STATUS_PATH\n"
     "    h_ctl = None  # CONTRACT (live add): channel to the handoff process for an added camera's connection\n"),
    ("        h_stop = ctx.Event()\n",
     "        h_stop = ctx.Event()\n"
     "        h_ctl, h_ctl_child = ctx.Pipe()\n"),
    ("SOCKET_PATH, STATUS_PATH, h_stop, HERE))", "SOCKET_PATH, STATUS_PATH, h_stop, HERE, h_ctl_child))"),
]

REVISION_CODE = """
    rev = {"mtime": os.stat(DESIRED).st_mtime_ns, "checked": 0.0}
    DEPLOYMENT = cams[0]["deployment_id"]
    METRICS = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"

    def _profiles(config):
        return sorted({p for b in config["analytics"]["v8_events"]["v8"]["bindings"] for p in b.get("embedding_profile_ids") or []})

    def worker_config(worker):  # as main() builds it
        config = {"camera_id": worker["camera_id"], "name": worker["name"], "enabled": True,
                  "processing": worker["processing"], "source": worker["source"], "analytics": worker["analytics"]}
        config["runtime_analytics"] = normalize_runtime_analytics(config)
        return config

    def stop_events(c, then):
        \"\"\"Stop c's event process in a thread (it drains as on shutdown), then run then() on the main loop thread.\"\"\"
        old = c["proc"]
        c["proc_restarting"] = True

        def stop_old():
            if old.is_alive():
                try:
                    c["inbox"].put(("stop",), timeout=5)
                except queue.Full:
                    pass
                old.join(timeout=20)
                if old.is_alive():
                    old.kill()
                    old.join(timeout=5)
            GLib.idle_add(lambda: then() and False)
        threading.Thread(target=stop_old, daemon=True, name=f"events-stop-{c['name']}").start()

    def start_events(c):
        \"\"\"Start c's event process from c["proc_args"]; main loop thread only (PR_SET_PDEATHSIG follows the creating thread).\"\"\"
        for q in (c["inbox"], ms_responses[c["name"]]):  # leftovers meant for an old process
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        sys.argv[:] = _DLS_ARGV
        c["proc"] = ctx.Process(target=contract_glue.child, name=f"events-{c['name']}", daemon=True, args=c["proc_args"])
        c["proc"].start()
        if c["bin"] is not None and c["seen"]:
            send(c, ("status", "healthy", "none"), droppable=False)
        elif c["bin"] is None and c.get("t0"):
            send(c, ("status", "unavailable", "stream_disconnected"), droppable=False)
        c["proc_restarting"] = False
        log(f"{c['name']}: event process started (pid {c['proc'].pid})")
        return True

    def restart_events(c, why):
        log(f"{c['name']}: restarting its event process ({why}); other cameras not touched")
        stop_events(c, lambda: start_events(c))

    def detach_source(c):
        if c["bin"] is None:
            return
        src = c["bin"].get_static_pad("src")
        c["bin"].set_state(Gst.State.NULL)
        src.unlink(c["sink"])
        c["bin"].get_static_pad("fire").unlink(c["fsink"])
        pipeline.remove(c["bin"])
        c["bin"], c["sink"], c["fsink"] = None, None, None

    def remove_camera(c):
        \"\"\"CONTRACT (live remove): only this camera stops; its branch stays idle for the next camera added.\"\"\"
        c["active"] = False
        cams.remove(c)
        detach_source(c)
        name = c["name"]

        def gone():
            safe = contract_glue.metric_name(name)
            for prefix in ("decoder", "camera-supervisor", "runtime-camera", "camera-health", "face", "vehicle-crossing"):
                try:
                    (METRICS / f"{prefix}-{safe}.json").unlink()
                except OSError:
                    pass
            contract_glue.unmark_ready(name)
            c["free"], c["proc_restarting"] = True, False
            log(f"{name}: removed from the deployment; branch {c['n']} idle and free for the next camera")
            return True
        stop_events(c, gone)

    class _Input:  # stands in for the queue element whose sink pad start() links the source bin to
        def __init__(self, pad):
            self.pad = pad

        def get_static_pad(self, _name):
            return self.pad

    def build_branch(c):
        \"\"\"One new branch in the running graph: the elements main() builds per camera (branch_parts) and the same
        C steps attached to them; models are the shared instances already loaded (model-instance-id, C globals).\"\"\"
        n = c["n"]
        if os.environ.get("DLS_SHM_DIR", "/dev/shm") == "/dev/shm":
            st = os.statvfs("/dev/shm")
            if st.f_bavail * st.f_frsize < contract_glue.SHM_PER_CAMERA:
                raise RuntimeError("/dev/shm has no room for another camera's shared memory")
        branch = Gst.parse_bin_from_description(" ".join(branch_parts(n, [])), False)
        branch.set_name(f"branch{n}")
        branch.add_pad(Gst.GhostPad.new("sink", branch.get_by_name(f"min{n}").get_static_pad("sink")))
        branch.add_pad(Gst.GhostPad.new("fire_sink", branch.get_by_name(f"fin{n}").get_static_pad("sink")))
        c["mux"], c["fmux"] = _Input(branch.get_static_pad("sink")), _Input(branch.get_static_pad("fire_sink"))
        pipeline.add(branch)
        g = lambda name: pipeline.get_by_name(f"{name}{n}")
        src = lambda name: ctypes.c_void_p(hash(g(name).get_static_pad("src")))
        sinkpad = lambda name: ctypes.c_void_p(hash(g(name).get_static_pad("sink")))
        strip.strip_batch_meta(sinkpad("mc"))
        c["evt"], c["pix"], c["fevt"] = g("evt"), g("pix"), g("fevt")
        cg.cgates_attach_dedup_real(src("vd"))
        cg.cgates_attach_face(src("fs"), int(os.getenv("GATE_FACE_MIN_PX", "28")))
        if not cg.cgates_attach_pose_direct(src("hp"), os.path.join(HERE, "models", "best_a0_fp16.xml").encode(), pose_device.encode(), None):
            raise RuntimeError("head-pose step not attached")
        c["cface"] = ctypes.c_void_p(cg.cface_new())
        cg.cface_attach_landmarks(c["cface"], src("lm"))
        if not cg.cface_attach_embed(c["cface"], src("ae"), ada, os.getenv("ADA_DEVICE", "NPU").encode()):
            raise RuntimeError("AdaFace step not attached")
        cg.cface_attach_json(c["cface"], src("mc"))
        if PLATE_SELECT:  # option A plate steps, as main() attaches them per camera (otherwise this camera reads no plates)
            sel = cg.cplate_attach_select(src("pv"), PLATE_VEH_MIN_W)
            cg.cplate_config_change(ctypes.c_void_p(sel), int(os.getenv("PLATE_C_EVERY") or 0), int(os.getenv("PLATE_CHANGE") == "1"),
                                    float(os.getenv("PLATE_CHANGE_MOVE") or 0.15), float(os.getenv("PLATE_CHANGE_GROW") or 1.25),
                                    float(os.getenv("PLATE_CHANGE_MAX_S") or 3.0))
            if plate_state:
                cg.cplate_attach_report(ctypes.c_void_p(sel), src("pr"), stable_s, confirm)
        strip.strip_batch_meta(sinkpad("fmc"))
        c["shm"] = shared_memory.SharedMemory(create=True, size=event_worker.shm_bytes())
        c["hdr"] = np.ndarray((128,), dtype=np.int64, buffer=c["shm"].buf)
        c["hdr"][:] = 0
        c["creader"] = cg.creader_new(ctypes.addressof(ctypes.c_char.from_buffer(c["shm"].buf)),
                                      LAY["ps"], LAY["pb"], LAY["js"], LAY["jb"], LAY["fs"], LAY["fb"])
        if os.getenv("PIX_ON_DEMAND") == "1":
            cg.creader_set_ondemand(c["creader"], int(os.getenv("PIX_HELD") or 16))  # her default (4d563b5)
        if JPEG_OD:
            cg.creader_set_jpeg_ondemand(c["creader"], ctypes.c_void_p(hash(g("jsrc"))), sinkpad("jpg"))
        if os.getenv("PIX_JPEG") == "1" and not JPEG_OD:
            cg.creader_set_jpeg(c["creader"])
        no_pix = os.getenv("NO_PIX") == "1" or os.getenv("JPEG_GPU_RATE") or int(os.getenv("EXPORT_EVERY") or 0)
        cg.creader_attach(c["creader"], sinkpad("evt"), None if no_pix else sinkpad("pix"), sinkpad("fevt"))
        if steady["t"] is not None:
            c["hdr"][7] = pipeline.get_base_time()
        sec = sys.modules.get("sec_gates")
        if sec is not None:  # secondary (plates) branch: frame scale of this camera key (ingest_config width 1920)
            sec._scale[f"camera-{n + 1:02d}"] = sec.scale_for(1920) if hasattr(sec, "scale_for") else 640.0 / 1920
        branch.sync_state_with_parent()
        if h_ctl is None:
            raise RuntimeError("handoff scheduler runs in the engine process (HANDOFF_PROCESS!=1): no live add")
        parent_end, child_end = ctx.Pipe(duplex=True)
        h_ctl.send((f"branch{n}", parent_end))
        c["handoff_child"] = child_end

    def add_camera(worker):
        \"\"\"CONTRACT (live add): the new camera gets a free idle branch or a new one; the other cameras are not touched.\"\"\"
        free = [s for s in slots if s.get("free")]
        if free:
            c = free[0]
            how = f"idle branch {c['n']} reused"
        else:
            c = {"n": len(slots), "in": 0, "out": 0, "fire": 0, "was_in": 0, "was_out": 0, "was_fire": 0,
                 "shm_size": None, "rt": {}}
            build_branch(c)
            slots.append(c)
            how = f"new branch {c['n']} built"
        config = worker_config(worker)
        c.update({"name": worker["camera_id"], "url": worker["source"]["uri"], "fps": int(round(worker["processing"]["fps"])),
                  "config": config, "deployment_id": config["analytics"]["v8_events"]["v8"]["deployment_id"],
                  "bin": None, "sink": None, "fsink": None, "rebuilds": 0, "wait": 1, "retry_at": 0, "last": 0, "seen": False,
                  "t0": 0, "grace": 120.0, "queued": 0, "dropped": 0, "no_pixels": 0, "reader_ms": 0.0, "frame_count": 0,
                  "index": 0, "slot": -1, "size": None, "last_slot": None, "restarts": 0, "was_done": 0.0,
                  "active": True, "free": False, "proc_restarting": False, "in_at_start": int(c["hdr"][3])})
        if "handoff_child" not in c:  # a branch from start: its handoff connection is the one main() gave it
            c["handoff_child"] = c["proc_args"][5]
        if c["name"] not in ms_responses:  # model-server reply channel for a camera id it has not seen
            reader_end, writer_end = ctx.Pipe(duplex=False)
            ms_responses[c["name"]] = contract_glue.ConnReply(reader_end)
            ms_requests.put(("__add__", c["name"], contract_glue.ConnReply(writer_end)))
        c["inbox"] = ctx.Queue(QUEUE_MAX)
        c["stats"] = ctx.Array("d", len(event_worker.STATS), lock=False)
        if steady["t"] is not None:
            c["stats"][event_worker.STATS.index("steady_at")] = steady["t"]
            c["s0"], c["glat"] = {"out": int(c["hdr"][0]), "fire": int(c["hdr"][1]), "done": 0.0}, []
        c["proc_args"] = ("event_worker_shm", "run", c["name"], c["config"], c["deployment_id"], c["handoff_child"],
                          c["inbox"], c["stats"], dict(paths, model_server=(ms_requests, ms_responses[c["name"]]),
                                                       model_info=model_info), FACE_INTERVAL, c["shm"].name)
        cams.append(c)
        start_events(c)
        if stream_ready(c["url"]):  # as at start: reachable -> started now; not reachable -> reported unavailable, retried
            start(c)
        else:
            c["retry_at"] = time.time() + c["wait"]
            send(c, ("status", "unavailable", "stream_disconnected"), droppable=False)
            log(f"{c['name']}: source not reachable; retrying")
            how += ", source not reachable yet"
        return how

    def apply_revision():
        \"\"\"CONTRACT desiredState: a new valid revision, applied camera by camera without disturbing the others.\"\"\"
        now = time.time()
        if now - rev["checked"] < 2.0:
            return
        rev["checked"] = now
        try:
            mtime = os.stat(DESIRED).st_mtime_ns
            if mtime == rev["mtime"]:
                return
            workers = json.loads(Path(DESIRED).read_text(encoding="utf-8"))["cameras"]
        except (OSError, ValueError, KeyError):
            return  # being replaced: next check
        rev["mtime"] = mtime
        if any(w["analytics"]["v8_events"]["v8"]["deployment_id"] != DEPLOYMENT for w in workers):
            log("new revision changes the deployment: the supervisor restarts the worker")
            return
        by_id = {w["camera_id"]: w for w in workers}
        changed = []
        for c in [c for c in cams if c["name"] not in by_id]:
            remove_camera(c)
            changed.append(f"{c['name']}: removed")
        for c in list(cams):
            worker = by_id[c["name"]]
            config = worker_config(worker)
            if config == c["config"]:
                continue
            url, fps = worker["source"]["uri"], int(round(worker["processing"]["fps"]))
            source_changed = url != c["url"] or fps != c["fps"]
            restart = (source_changed or _profiles(config) != _profiles(c["config"])
                       or set(config["analytics"]) != set(c["config"]["analytics"]))
            c["config"] = config
            args = list(c["proc_args"])
            args[3] = config  # ("event_worker_shm", "run", name, config, ...)
            c["proc_args"] = tuple(args)
            if source_changed:
                c["url"], c["fps"] = url, fps
                if c["bin"] is not None:
                    remove(c, "new revision: source or fps changed")
                c["retry_at"], c["wait"] = 0, 1  # rebuilt by the watchdog as soon as the source answers
                changed.append(f"{c['name']}: source/fps -> {fps} fps")
            if restart:
                restart_events(c, "new revision: fps, source or embedding profiles changed")
                if not source_changed:
                    changed.append(f"{c['name']}: event process restarted (profiles/apps)")
            else:
                send(c, ("config", config), droppable=False)
                changed.append(f"{c['name']}: settings applied live")
        present = {c["name"] for c in cams}
        for worker in workers:
            if worker["camera_id"] in present:
                continue
            try:
                changed.append(f"{worker['camera_id']}: added ({add_camera(worker)})")
            except Exception as exc:  # noqa: BLE001
                log(f"FATAL camera {worker['camera_id']} could not be added live ({type(exc).__name__}: {exc}); "
                    "the worker restarts with the new revision")
                status[0] = 6
                loop.quit()
                return
        log(f"new revision applied per camera: {changed or 'no camera changed'}")

    def watchdog():
"""

patch(root / "integrate9.py", GRAPH_EDITS + [
    ("\n    def watchdog():\n", REVISION_CODE),
    ("            log(\"stop requested by the supervisor\")\n            loop.quit()\n            return False\n        now = time.time()\n",
     "            log(\"stop requested by the supervisor\")\n            loop.quit()\n            return False\n"
     "        apply_revision()\n        now = time.time()\n"),
])

patch(root / "event_worker_shm.py", [
    # CONTRACT desiredState (per camera, live): the engine sends this camera's new settings; same as V8's worker
    # (_refresh_camera_config_if_changed + V8EventPipeline.reconfigure). The dict is updated in place, so her
    # FaceSamplePipeline (holds the same dict) sees the new settings too.
    ("            if message[0] == \"plates\":",
     "            if message[0] == \"config\":\n"
     "                try:\n"
     "                    for _key in (\"analytics\", \"runtime_analytics\", \"processing\", \"source\"):\n"
     "                        config[_key] = message[1][_key]\n"
     "                    v8.reconfigure(config)\n"
     "                    _status.telemetry.use_case_catalog = {str(b[\"use_case_id\"]): str(b[\"app\"]) for b in v8.bindings}\n"
     "                    print(f\"{time.strftime('%T')} {name}: settings of revision {v8.config['config_revision']} applied live\", flush=True)\n"
     "                except Exception as exc:\n"
     "                    add(\"errors\", 1)\n"
     "                    print(f\"{time.strftime('%T')} {name}: settings not applied: {type(exc).__name__}: {exc}\", flush=True)\n"
     "            if message[0] == \"plates\":"),

    ("    v8 = V8EventPipeline(name, config, scene=scene, body=body, gait=gait, telemetry=None,",
     "    import contract_glue\n"
     "    _status_mem = shared_memory.SharedMemory(name=shm_name)\n"
     "    _status = contract_glue.CameraStatus(name, fps, v8conf[\"bindings\"], np.ndarray((128,), dtype=np.int64, buffer=_status_mem.buf))\n"
     "    v8 = V8EventPipeline(name, config, scene=scene, body=body, gait=gait, telemetry=_status.telemetry,  # CONTRACT: V8 telemetry"),
    ("    s = {k: i for i, k in enumerate(STATS)}\n",
     "    s = {k: i for i, k in enumerate(STATS)}\n"
     "    _status.attach(v8.session_id)  # CONTRACT: decoder status carries her stream session id\n"),
    ("                    session = v8.camera_status(message[1], message[2])\n",
     "                    session = v8.camera_status(message[1], message[2])\n"
     "                    _status.camera_status(message[1], message[2], session)\n"),
    ("                    add(\"frames\", 1)\n",
     "                    add(\"frames\", 1)\n"
     "                    _status.frame()\n"),
    ("                    if needed and frame is None:\n"
     "                        add(\"skipped\", 1)\n"
     "                        add(\"no_pixels\", 1)\n",
     "                    if needed and frame is None:\n"
     "                        add(\"skipped\", 1)\n"
     "                        add(\"no_pixels\", 1)\n"
     "                        _status.skipped(\"frames_skipped_pixels_not_held\")\n"),
    # Scene: her every-7th rule is kept as she wrote it (user decision 2026-11-18: it overrides the desired-state interval).
    ("        if not did:\n            time.sleep(0.002)\n",
     "        _status.tick()\n"
     "        if not did:\n            time.sleep(0.002)\n"),
])

# CONTRACT (live add): the handoff process takes connections of cameras added later (control pipe), and her scheduler's
# dispatch loop re-reads its connection list each pass (it read it once at start). Exact-once source edit, as her own
# policy patches do; nothing else in the scheduler changes.
patch(root / "handoff_host.py", [
    ("def serve(parent, socket_path, status_path, stop, here):\n",
     "def serve(parent, socket_path, status_path, stop, here, control=None):\n"),
    ("    from pipeline.sentinel_v8_handoff import V8HandoffScheduler\n",
     "    from pipeline.sentinel_v8_handoff import V8HandoffScheduler\n"
     "    import inspect\n"
     "    import textwrap\n"
     "    _src = textwrap.dedent(inspect.getsource(hv.V8HandoffScheduler._dispatch))\n"
     "    _old = \"    connections = list(self.camera_connections.values())\\n    while not self.stop_requested.is_set():\\n\"\n"
     "    _new = \"    while not self.stop_requested.is_set():\\n        connections = list(self.camera_connections.values())\\n\"\n"
     "    if _src.count(_old) != 1:\n"
     "        raise RuntimeError(\"live camera add: her scheduler's dispatch loop changed, patch not applied\")\n"
     "    _ns = dict(hv.__dict__)\n"
     "    exec(_src.replace(_old, _new), _ns)\n"
     "    hv.V8HandoffScheduler._dispatch = _ns[\"_dispatch\"]\n"),
    ("    scheduler = V8HandoffScheduler(parent, socket_path, status_path).start()\n",
     "    scheduler = V8HandoffScheduler(parent, socket_path, status_path).start()\n"
     "    if control is not None:\n"
     "        import threading\n"
     "\n"
     "        def _add_cameras():\n"
     "            while not stop.is_set():\n"
     "                try:\n"
     "                    if not control.poll(1.0):\n"
     "                        continue\n"
     "                    key, conn = control.recv()\n"
     "                except (EOFError, OSError):\n"
     "                    return\n"
     "                if os.getenv(\"PKG_SHM\") == \"1\":\n"
     "                    import pkgshm\n"
     "                    conn = pkgshm.HostConn(conn)\n"
     "                scheduler.camera_connections[key] = conn\n"
     "                print(f\"{time.strftime('%T')} handoff: connection for {key} added\", flush=True)\n"
     "        threading.Thread(target=_add_cameras, daemon=True, name=\"handoff-add\").start()\n"),
])

# CONTRACT (live add): the shared model server learns the reply channel of a camera added later.
patch(root / "model_server.py", [
    ("        if message is None:\n            break\n",
     "        if message is None:\n            break\n"
     "        if message[0] == \"__add__\":  # reply channel of a camera added while running\n"
     "            responses[message[1]] = message[2]\n"
     "            continue\n"),
])

# CONTRACT (live add onto a reused idle branch): the camera's event process starts reading the shared-memory rings at
# their current position. Before, a camera added onto a removed camera's idle branch first replayed that camera's last
# ring messages as its own events (seen 2026-11-24: first packages ~60 s old, from the removed camera). A fresh branch
# starts at 0 anyway; a restarted event process now skips what is already in the ring instead of replaying it.
patch(root / "event_worker_shm.py", [
    ("    last_main = last_fire = frame_count = 0\n",
     "    last_main, last_fire, frame_count = int(H[0]), int(H[1]), 0  # CONTRACT: no replay of a previous camera's messages\n"),
])
