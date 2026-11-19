"""Camera worker for the V8 supervisor (traffic_pilot_runtime/solution_image_entrypoint.py, unchanged).

The supervisor always starts <repo_root>/services/worker/stream_fleet_openvino.py; in this image that is this shim.
It starts Chandana's engine (integrate9.py, contract-glued copy) in the same process (exec, so the supervisor's
pid and stop handling stay correct), with the environment her measured runs used: `env -i` + DL Streamer's
setup_dls_env.sh + her switches (run_best20.sh / run_full9.sh / run_integrate.sh) + the supervisor's contract variables.
"""
import json
import os
import sys

ROOT = os.environ["DLS_INTEGRATE_ROOT"]  # .../chandana_optimization (her layout, setup_layout.sh)
ENGINE = os.path.join(ROOT, "integrate", "integrate9.py")
GENERATED = os.environ.get("APEXFABRIC_GENERATED_DIR", "/tmp/sentinel/generated")

# supervisor -> worker (V8 contract runtime) and delivery
PASS = ["CAMERAS_FILE", "APEXFABRIC_CAMERA_READY_DIR", "APEXFABRIC_WORKER_STOP_FILE", "APEXFABRIC_STATE_ROOT",
        "APEXFABRIC_GENERATED_DIR", "SNAPSHOT_ROOT", "SENTINEL_DELIVERY_AGENT_SOCKET", "SENTINEL_CV_HANDOFF_STATUS_PATH",
        "SENTINEL_TELEMETRY_INTERVAL_SECONDS", "ANALYTICS_EVENT_LOG_PATH", "EDGE_ID", "TZ",
        # her final switches (image ENV, see Dockerfile)
        "HANDOFF_PROCESS", "HANDOFF_QUEUE", "PIX_VIA_MAP", "CFACES", "POLICY_OURS", "PIX_JPEG", "PIX_JPEG_OD",
        "PIX_ON_DEMAND", "PIX_LAZY", "REGION_DECODE", "PKG_SHM", "VEH_MODEL", "CPU_OPT", "CPU_OPT_THREADS",
        "SCENE_DEVICE", "BODY_DEVICE", "GAIT_DEVICE", "HEAD_POSE_DEVICE", "WARMUP_S", "T_GAIT_SCALE"]
# DL Streamer 2026.2 setup_dls_env.sh (Ubuntu branch), as her runs sourced it
DLS = {
    "LIBVA_DRIVER_NAME": "iHD", "GST_VA_ALL_DRIVERS": "1", "LIBVA_DRIVERS_PATH": "/usr/lib/x86_64-linux-gnu/dri",
    "GST_PLUGIN_PATH": "/opt/intel/dlstreamer/lib:/opt/intel/dlstreamer/gstreamer/lib/gstreamer-1.0:/opt/intel/dlstreamer/gstreamer/lib/",
    "LD_LIBRARY_PATH": "/opt/intel/dlstreamer/gstreamer/lib:/opt/intel/dlstreamer/lib:/opt/intel/dlstreamer/lib/gstreamer-1.0:/usr/lib:"
                       "/usr/local/lib/gstreamer-1.0:/usr/local/lib:/opt/opencv:/opt/rdkafka:/opt/librealsense",
    "PATH": "/opt/intel/dlstreamer/gstreamer/bin:/opt/intel/dlstreamer/bin:/usr/local/bin:/usr/bin:/bin",
    "PYTHONPATH": "/opt/intel/dlstreamer/gstreamer/lib/python3/dist-packages:/opt/intel/dlstreamer/python",
    "GI_TYPELIB_PATH": "/opt/intel/dlstreamer/gstreamer/lib/girepository-1.0:/opt/intel/dlstreamer/lib/girepository-1.0:"
                       "/usr/lib/x86_64-linux-gnu/girepository-1.0",
}


def main():
    cache = os.path.join(os.path.dirname(GENERATED.rstrip("/")) or "/tmp/sentinel", "ov_cache")  # target of ROOT/ov_cache
    os.makedirs(cache, exist_ok=True)
    os.makedirs(GENERATED, exist_ok=True)
    # Secondary branch (plates + OCR) camera list, generated from the supervisor's cameras instead of her 20-camera
    # test file; 1920x1080 is what her gates were measured with (sec_gates.py scales by 640/width).
    cameras = json.load(open(os.environ["CAMERAS_FILE"], encoding="utf-8"))["cameras"]
    ingest = {"cameras": [{"name": c["camera_id"], "url": "", "width": 1920, "height": 1080} for c in cameras],
              "models_dir": os.path.join(ROOT, "group_ingest", "models"), "procs_dir": os.path.join(ROOT, "group_ingest"),
              "max_group_size": 1, "frozen_after_s": 60, "min_free_gb": 5, "publish": {"method": "null"}}
    ingest_path = os.path.join(GENERATED, "ingest_config.json")
    with open(ingest_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(ingest, f)
    os.replace(ingest_path + ".tmp", ingest_path)

    env = {k: os.environ[k] for k in PASS if os.environ.get(k) is not None}
    env.update(DLS)
    env.update(HOME=os.environ["HOME"], USER="apexruntime", PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1",
               GST_REGISTRY=os.path.join(GENERATED, "gst-registry.bin"), INGEST_CONFIG=ingest_path, OV_CACHE_DIR=cache,
               XDG_CACHE_HOME=os.path.join(os.path.dirname(GENERATED.rstrip("/")), "cache"),
               TMPDIR=os.path.join(os.path.dirname(GENERATED.rstrip("/")), "tmp"))  # read-only root: temp files in /tmp/sentinel
    os.makedirs(env["TMPDIR"], exist_ok=True)
    os.chdir(os.path.dirname(ENGINE))
    sys.stdout.flush()
    os.execve("/usr/bin/python3", ["/usr/bin/python3", "-u", "-s", ENGINE, os.environ["CAMERAS_FILE"], "0"], env)


if __name__ == "__main__":
    main()
