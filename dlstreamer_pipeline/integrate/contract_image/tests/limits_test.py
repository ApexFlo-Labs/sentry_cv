#!/usr/bin/env python3
"""Contract resources: N cameras inside the contract's limits (cpu limit 12, memory limit 14Gi), default /dev/shm (64 MB),
read-only root, uid 10001. Records: peak container memory, /dev/shm use, OOM/SIGBUS/FATAL, per-camera requested vs
delivered fps and engine drops (from /healthz telemetry), agent-rejected packages.
Not a capacity qualification (.92, turbo on, no isolation).
Usage: limits_test.py <image> <desired_state.json (2 cams)> <fixture.mp4> <outdir> <cameras> [seconds]
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE, BASE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
N, SECONDS = int(sys.argv[5]), int(sys.argv[6]) if len(sys.argv) > 6 else 300
REPO, HERE = Path.home() / "Documents/sentry_cv", Path(__file__).resolve().parent
PORT, NAME, PORT0 = 18497, "dlsint-limits", 19901


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def build_state(base):
    template = base["cameras"][0]
    tid, cams = template["camera_id"], []
    for i in range(N):
        cid = f"cam{i + 1:02d}"
        cam = json.loads(json.dumps(template).replace(tid, cid))
        cam.pop("fps", None)  # contract default 8 fps
        geo = cam.get("geometry") or {}
        gmap = {g["id"]: f"{cid}-{g['id']}" for k in ("zones", "lines", "counting_lines") for g in geo.get(k) or []}
        umap = {b["use_case_id"]: f"{cid}-{b['use_case_id']}" for b in cam.get("bindings") or []}
        for k in ("zones", "lines", "counting_lines"):
            for g in geo.get(k) or []:
                g["id"] = gmap[g["id"]]
        for b in cam.get("bindings") or []:
            b["use_case_id"] = umap[b["use_case_id"]]
            b["geometry_ids"] = [gmap.get(x, x) for x in b.get("geometry_ids") or []]
            if "scene_trigger_use_case_ids" in b:
                b["scene_trigger_use_case_ids"] = [umap.get(x, x) for x in b["scene_trigger_use_case_ids"]]
        cams.append(cam)
    return dict(base, cameras=cams, revision=1)


def main():
    for d in ("config", "secrets", "delivery"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    state = build_state(json.loads(BASE.read_text()))
    (OUT / "config/desired_state.json").write_text(json.dumps(state, indent=1))
    (OUT / "agent-mode").write_text("ok")
    procs = []
    for i, cam in enumerate(state["cameras"]):
        (OUT / "secrets" / f"{cam['camera_id']}.url").write_text(f"tcp://127.0.0.1:{PORT0 + i}\n")
        procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"), "--fixture",
                                       FIXTURE, "--port", str(PORT0 + i)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen(["python3", str(HERE / "fault_agent.py"), "--socket", str(OUT / "delivery/agent.sock"),
                                   "--schemas", str(REPO / "newdetails/sentry-v8"), "--log", str(OUT / "packages.jsonl"),
                                   "--status", str(OUT / "agent-status.json"), "--mode-file", str(OUT / "agent-mode"),
                                   "--attempts", str(OUT / "attempts.jsonl")], stdout=open(OUT / "agent.log", "wb"),
                                  stderr=subprocess.STDOUT))
    time.sleep(3)
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    samples = []
    try:
        subprocess.run(["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777", "--cpus", os.getenv("CPUS", "12"), "--memory", os.getenv("MEMORY_LIMIT", "14g"), "--pids-limit", os.getenv("PIDS_LIMIT", "2048"),
                        "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
                        "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
                        "-e", f"APEX_API_PORT={PORT}", "-v", f"{OUT / 'config'}:/configs:ro",
                        "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro", "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery",
                        IMAGE], check=True, capture_output=True)
        t0 = time.time()
        while time.time() - t0 < 900 and not get("/readyz").get("ready"):
            time.sleep(3)
        ready_after = time.time() - t0
        time.sleep(60)  # warm-up
        end = time.time() + SECONDS
        while time.time() < end:
            mem = subprocess.run(["podman", "stats", "--no-stream", "--format", "{{.MemUsage}}", NAME], capture_output=True, text=True).stdout.strip()
            shm = subprocess.run(["podman", "exec", NAME, "df", "-k", "/dev/shm"], capture_output=True, text=True).stdout.split("\n")[1].split()
            fshm = subprocess.run(["podman", "exec", NAME, "du", "-sk", "/tmp/sentinel/shm"], capture_output=True, text=True).stdout.split()
            threads = subprocess.run(["podman", "exec", NAME, "sh", "-c", "ls -d /proc/[0-9]*/task/* | wc -l"], capture_output=True, text=True).stdout.strip()
            samples.append({"threads": int(threads or 0), "t": round(time.time() - t0), "mem": mem, "shm_used_kb": int(shm[2]), "shm_size_kb": int(shm[1]),
                            "file_shm_used_kb": int(fshm[0]) if fshm else 0})
            time.sleep(15)
        health = get("/healthz")
        (OUT / "healthz-final.json").write_text(json.dumps(health, indent=1))
        state_h = {c["camera_id"]: c["state"] for c in get("/readyz").get("per_camera_state") or []}
        inspect = json.loads(subprocess.run(["podman", "inspect", NAME], capture_output=True, text=True).stdout)[0]["State"]
    finally:
        log = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (OUT / "container.log").write_text(log.stderr + log.stdout)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        for p in procs:
            p.terminate()
    cams = {}
    for c in (health.get("telemetry") or {}).get("cameras") or []:
        g = (c.get("processing") or {}).get("gauges") or {}
        cams[c["camera_id"]] = {k: round(g[k], 2) for k in ("requested_fps", "engine_input_fps", "engine_output_fps",
                                                          "engine_dropped_fps", "delivered_fraction_of_requested") if k in g}
    text = (OUT / "container.log").read_text()
    attempts = [json.loads(l) for l in (OUT / "attempts.jsonl").read_text().splitlines()]
    out = {"image": IMAGE, "cameras": N, "limits": {"cpus": 12, "memory": os.getenv("MEMORY_LIMIT", "14g"), "shm": "default"},
           "ready_after_s": round(ready_after), "oom_killed": inspect.get("OOMKilled"), "exit_code": inspect.get("ExitCode"),
           "running_at_end": inspect.get("Running"), "camera_states_end": state_h, "per_camera_fps": cams,
           "peak_shm_used_mb": round(max(s["shm_used_kb"] for s in samples) / 1024, 1) if samples else None,
           "shm_size_mb": round(samples[0]["shm_size_kb"] / 1024) if samples else None,
           "peak_tmp_sentinel_shm_used_mb": round(max(s["file_shm_used_kb"] for s in samples) / 1024, 1) if samples else None,
           "shared_memory_line": next((l for l in (OUT / "container.log").read_text().splitlines() if "shared memory:" in l), None),
           "memory_samples": [s["mem"] for s in samples][::4],
           "peak_threads": max((s["threads"] for s in samples), default=None), "pids_limit": os.getenv("PIDS_LIMIT", "2048"),
           "fatal_or_bus_errors": re.findall(r"FATAL.*|Bus error.*|SIGBUS.*|Traceback.*|MemoryError.*", text)[:5],
           "packages_accepted": sum(1 for a in attempts if a["status"] in (200, 201)),
           "packages_rejected_invalid": sum(1 for a in attempts if a.get("invalid"))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
