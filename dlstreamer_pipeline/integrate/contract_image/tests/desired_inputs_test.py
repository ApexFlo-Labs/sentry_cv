#!/usr/bin/env python3
"""Desired-state input -> CV -> packages output, on one running container.

Sends a sequence of valid revisions (different scene intervals, changed zone names) and for each one checks:
  input side : revision applied (/healthz revision), worker running, both cameras healthy, ready
  output side: packages after the switch carry that config_revision, every package accepted by the
               schema-validating capture agent, no engine errors / FATAL / Traceback in the window,
               scene samples present, spacing (her every-7th rule) reported per revision
Usage: desired_inputs_test.py <image> <desired_state.json (2 cameras, SA)> <fixture.mp4> <outdir>
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

IMAGE, BASE_STATE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
REPO = Path.home() / "Documents/sentry_cv"
PORT, SRC = 18492, (19821, 19822)
NAME = "dlsint-desired-inputs"
WINDOW = 120  # seconds observed per revision after it is applied
results = []


def log(msg):
    line = f"{time.strftime('%T')} {msg}"
    print(line, flush=True)
    with open(OUT / "desired_inputs.log", "a") as f:
        f.write(line + "\n")


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def wait(fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(3)
    return None


def podman(*a):
    return subprocess.run(["podman", *a], capture_output=True, text=True)


def logs():
    r = podman("logs", NAME)
    return r.stdout + r.stderr


def packages():
    out = []
    try:
        for line in (OUT / "packages.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def agent():
    try:
        return json.loads((OUT / "agent-status.json").read_text())
    except (OSError, ValueError):
        return {}


def invalid_count():
    try:
        return (OUT / "agent.log").read_text(errors="replace").count("invalid_package")
    except OSError:
        return 0


def write_state(state):
    tmp = OUT / "config" / ".tmp.json"
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, OUT / "config" / "desired_state.json")


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def variant(base, revision, scene_interval, zone_suffix):
    s = json.loads(json.dumps(base))
    s["revision"] = revision
    for cam in s["cameras"]:
        for b in cam.get("bindings") or []:
            if b.get("app") == "scene_search":
                b["sample_interval_seconds"] = scene_interval
        for z in (cam.get("geometry") or {}).get("zones") or []:
            z["name"] = re.sub(r" \(r\d+\)$", "", z.get("name", "zone")) + f" (r{revision}{zone_suffix})"
    return s


def check_revision(revision, scene_interval, t_written):
    applied = wait(lambda: get("/healthz").get("revision") == revision and get("/readyz").get("ready"), 900)
    t_applied = time.time()
    log(f"revision {revision} (scene {scene_interval} s) applied={bool(applied)} after {t_applied - t_written:.0f} s")
    log_mark = len(logs())
    n0, inv0 = len(packages()), invalid_count()
    time.sleep(WINDOW)
    h = get("/readyz")
    states = {c["camera_id"]: c["state"] for c in h.get("per_camera_state") or []}
    window_log = logs()[log_mark:]
    errors = [l for l in window_log.splitlines()
              if re.search(r"FATAL|Traceback|event code error|reader error|desired_state_rejected|protocol_errors: [1-9]", l)]
    new = packages()[n0:]
    revs = sorted({p.get("config_revision") for p in new} - {None})
    scene = {}
    for p in new:
        if p.get("event_type") == "scene_sample":
            scene.setdefault(p["camera_id"], []).append(ts(p["observed_at"]))
    spacing = {cam: round((max(v) - min(v)) / max(1, len(v) - 1), 1) if len(v) > 1 else None for cam, v in scene.items()}
    a = agent()
    invalid = invalid_count() - inv0
    ok = (invalid == 0 and bool(applied) and revs == [revision] and all(s == "healthy" for s in states.values()) and len(states) == 2
          and not errors and len(new) > 0)
    results.append({"revision": revision, "scene_interval_requested": scene_interval, "passed": ok,
                    "applied_seconds": round(t_applied - t_written, 1), "camera_states": states,
                    "packages_in_window": len(new), "config_revisions_seen": revs,
                    "scene_samples": {k: len(v) for k, v in scene.items()}, "scene_mean_spacing_s": spacing,
                    "invalid_packages_rejected_by_agent": invalid, "engine_errors": errors[:5], "agent_packages_total": a.get("packages")})
    log(f"{'PASS' if ok else 'FAIL'} revision {revision}: cameras {states}; {len(new)} packages, revisions seen {revs}; "
        f"scene samples {({k: len(v) for k, v in scene.items()})}, mean spacing {spacing} s (requested {scene_interval} s); "
        f"engine errors {len(errors)}; packages rejected by agent {invalid}")
    for e in errors[:5]:
        log("   " + e[:250])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for d in ("config", "secrets", "delivery"):
        (OUT / d).mkdir(exist_ok=True)
    base = json.loads(BASE_STATE.read_text())
    cams = [c["camera_id"] for c in base["cameras"]]
    procs = []
    for cam, port in zip(cams, SRC):
        (OUT / "secrets" / f"{cam}.url").write_text(f"tcp://127.0.0.1:{port}\n")
        procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                       "--fixture", FIXTURE, "--port", str(port)],
                                      stdout=open(OUT / f"source-{port}.log", "wb"), stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/v8_capture_agent.py"),
                                   "--socket", str(OUT / "delivery/agent.sock"), "--schemas", str(REPO / "newdetails/sentry-v8"),
                                   "--log", str(OUT / "packages.jsonl"), "--status", str(OUT / "agent-status.json")],
                                  stdout=open(OUT / "agent.log", "wb"), stderr=subprocess.STDOUT))
    plan = [(1, 2, ""), (2, 30, ""), (3, 60, ""), (4, 2, "-zones")]
    write_state(variant(base, *plan[0]))
    time.sleep(3)
    podman("rm", "-f", NAME)
    run = ["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777",
           "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
           "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
           "-e", f"APEX_API_PORT={PORT}",
           "-v", f"{OUT / 'config'}:/configs:ro", "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro",
           "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery", IMAGE]
    try:
        subprocess.run(run, check=True, capture_output=True)
        t = time.time()
        check_revision(1, 2, t)
        for revision, interval, suffix in plan[1:]:
            write_state(variant(base, revision, interval, suffix))
            check_revision(revision, interval, time.time())
    finally:
        (OUT / "container.log").write_text(logs())
        podman("rm", "-f", NAME)
        for p in procs:
            p.terminate()
        a = agent()
        summary = {"image": IMAGE, "results": results,
                   "agent": {k: a.get(k) for k in ("packages", "embeddings", "evidence_bytes", "cameras")}, "invalid_packages_total": invalid_count()}
        (OUT / "results.json").write_text(json.dumps(summary, indent=1))
        log(f"done: {sum(r['passed'] for r in results)}/{len(results)} revisions passed; agent {json.dumps(summary['agent'])}")


if __name__ == "__main__":
    main()
