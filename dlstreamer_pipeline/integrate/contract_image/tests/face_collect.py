#!/usr/bin/env python3
"""Runs the image on a face video and saves every accepted face vector + its evidence crop (fault_agent --save-faces).
Usage: face_collect.py <image> <desired_state.json> <fixture.mp4> <outdir> [seconds]"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE, BASE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
SECONDS = int(sys.argv[5]) if len(sys.argv) > 5 else 360
REPO, HERE = Path.home() / "Documents/sentry_cv", Path(__file__).resolve().parent
PORT, SRC, NAME = 18494, (19841, 19842), "dlsint-face-collect"
for d in ("config", "secrets", "delivery"):
    (OUT / d).mkdir(parents=True, exist_ok=True)
state = json.loads(BASE.read_text())
(OUT / "config/desired_state.json").write_text(json.dumps(state))
(OUT / "agent-mode").write_text("ok")
procs = []
for cam, port in zip([c["camera_id"] for c in state["cameras"]], SRC):
    (OUT / "secrets" / f"{cam}.url").write_text(f"tcp://127.0.0.1:{port}\n")
    procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"), "--fixture", FIXTURE,
                                   "--port", str(port)], stdout=open(OUT / f"source-{port}.log", "wb"), stderr=subprocess.STDOUT))
procs.append(subprocess.Popen(["python3", str(HERE / "fault_agent.py"), "--socket", str(OUT / "delivery/agent.sock"),
                               "--schemas", str(REPO / "newdetails/sentry-v8"), "--log", str(OUT / "packages.jsonl"),
                               "--status", str(OUT / "agent-status.json"), "--mode-file", str(OUT / "agent-mode"),
                               "--attempts", str(OUT / "attempts.jsonl"), "--save-faces", str(OUT / "faces")],
                              stdout=open(OUT / "agent.log", "wb"), stderr=subprocess.STDOUT))
time.sleep(3)
subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
try:
    subprocess.run(["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777",
                    "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
                    "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
                    "-e", f"APEX_API_PORT={PORT}", "-v", f"{OUT / 'config'}:/configs:ro",
                    "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro", "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery",
                    IMAGE], check=True, capture_output=True)
    time.sleep(SECONDS)
finally:
    (OUT / "container.log").write_text(subprocess.run(["podman", "logs", NAME], capture_output=True, text=True).stderr +
                                       subprocess.run(["podman", "logs", NAME], capture_output=True, text=True).stdout)
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    for p in procs:
        p.terminate()
print("faces saved:", len(list((OUT / "faces").glob("*.json"))))
