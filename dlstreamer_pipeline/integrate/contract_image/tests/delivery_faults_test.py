#!/usr/bin/env python3
"""CV-side delivery contract under agent faults (contract delivery.cvRuntimeHandoff), her engine end to end.

Phases (agent mode): ok -> 429 -> ok -> 503 -> ok -> hang (no reply, > 30 s client timeout) -> ok.
Checks from the agent's attempt log + /healthz:
  D1 retries resend the exact same package (same package_id, same manifest and evidence hashes)
  D2 retry delays 250/500/1000/2000/4000 ms, at most 6 attempts per package
  D3 at most 4 handoffs in flight
  D4 packages that run out of attempts are reported (scheduler counters) and never accepted later as produced
  D5 cameras stay healthy during delivery faults (camera health is not delivery health)
  D6 delivery resumes after faults; no 409 conflicts, no invalid packages; no FATAL/Traceback
Usage: delivery_faults_test.py <image> <desired_state.json> <fixture.mp4> <outdir>
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE, BASE_STATE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
REPO = Path.home() / "Documents/sentry_cv"
HERE = Path(__file__).resolve().parent
PORT, SRC = 18493, (19831, 19832)
NAME = "dlsint-delivery-faults"
PHASES = [("ok", 60), ("429", 25), ("ok", 45), ("503", 25), ("ok", 45), ("hang", 75), ("ok", 75)]
results = []


def log(msg):
    line = f"{time.strftime('%T')} {msg}"
    print(line, flush=True)
    with open(OUT / "delivery_faults.log", "a") as f:
        f.write(line + "\n")


def check(case, ok, detail):
    results.append({"case": case, "passed": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'} {case}: {detail}")


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def podman(*a):
    return subprocess.run(["podman", *a], capture_output=True, text=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for d in ("config", "secrets", "delivery"):
        (OUT / d).mkdir(exist_ok=True)
    state = json.loads(BASE_STATE.read_text())
    state["revision"] = 1
    (OUT / "config" / "desired_state.json").write_text(json.dumps(state, indent=1))
    mode = OUT / "agent-mode"
    mode.write_text("ok")
    procs = []
    for cam, port in zip([c["camera_id"] for c in state["cameras"]], SRC):
        (OUT / "secrets" / f"{cam}.url").write_text(f"tcp://127.0.0.1:{port}\n")
        procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                       "--fixture", FIXTURE, "--port", str(port)],
                                      stdout=open(OUT / f"source-{port}.log", "wb"), stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["python3", str(HERE / "fault_agent.py"), "--socket", str(OUT / "delivery/agent.sock"),
                                   "--schemas", str(REPO / "newdetails/sentry-v8"), "--log", str(OUT / "packages.jsonl"),
                                   "--status", str(OUT / "agent-status.json"), "--mode-file", str(mode),
                                   "--attempts", str(OUT / "attempts.jsonl")],
                                  stdout=open(OUT / "agent.log", "wb"), stderr=subprocess.STDOUT))
    time.sleep(3)
    podman("rm", "-f", NAME)
    run = ["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777",
           "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
           "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
           "-e", f"APEX_API_PORT={PORT}",
           "-v", f"{OUT / 'config'}:/configs:ro", "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro",
           "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery", IMAGE]
    camera_samples, phase_marks = [], []
    try:
        subprocess.run(run, check=True, capture_output=True)
        t0 = time.time()
        while time.time() - t0 < 900 and not get("/readyz").get("ready"):
            time.sleep(3)
        time.sleep(30)  # warm-up
        for m, seconds in PHASES:
            mode.write_text(m)
            phase_marks.append((m, time.time()))
            log(f"agent mode {m} for {seconds} s")
            end = time.time() + seconds
            while time.time() < end:
                h = get("/readyz")
                camera_samples.append((time.time(), m, {c["camera_id"]: c["state"] for c in h.get("per_camera_state") or []}))
                time.sleep(5)
        health = get("/healthz")
        (OUT / "healthz-final.json").write_text(json.dumps(health, indent=1))
        container_log = podman("logs", NAME).stdout + podman("logs", NAME).stderr
    finally:
        (OUT / "container.log").write_text(podman("logs", NAME).stdout + podman("logs", NAME).stderr)
        podman("rm", "-f", NAME)
        for p in procs:
            p.terminate()

    attempts = [json.loads(l) for l in (OUT / "attempts.jsonl").read_text().splitlines()]
    by_pkg = {}
    for a in attempts:
        by_pkg.setdefault(a["package_id"], []).append(a)
    faulted = {k: v for k, v in by_pkg.items() if any(a["status"] in (429, 503, "hang") for a in v)}
    # D1 exact retry
    changed = [k for k, v in faulted.items() if len({(a["manifest_sha"], a["evidence_sha"]) for a in v}) != 1]
    check("D1 retries resend the exact same package", faulted and not changed,
          f"{len(faulted)} packages got a fault reply; {len(changed)} changed bytes between attempts")
    # D2 delays and attempt count
    expected = [0.25, 0.5, 1.0, 2.0, 4.0]
    bad_delay, over6, delays_seen = [], [], []
    for k, v in faulted.items():
        v = sorted(v, key=lambda a: a["mono"])
        if len(v) > 6:
            over6.append(k)
        for i in range(1, len(v)):
            prev = v[i - 1]
            gap = v[i]["mono"] - prev["mono"]
            if prev["status"] in (429, 503) and i - 1 < len(expected):
                delays_seen.append(round(gap, 2))
                if not (expected[i - 1] - 0.05 <= gap <= expected[i - 1] + 1.0):
                    bad_delay.append((k[:8], i, round(gap, 2)))
    check("D2 retry delays and at most 6 attempts", not bad_delay and not over6,
          f"delays seen (s, first 12) {delays_seen[:12]}; out-of-spec {bad_delay[:5]}; >6 attempts {len(over6)}")
    # D3 in flight
    max_active = max(a["active"] for a in attempts)
    check("D3 at most 4 handoffs in flight", max_active <= 4, f"max simultaneous requests at the agent {max_active}")
    # D4 exhausted reported, never later produced
    exhausted_pkgs = [k for k, v in faulted.items() if not any(a["status"] in (200, 201) for a in v)]
    counters = ((health.get("telemetry") or {}).get("handoff_scheduler") or {}).get("counters") or {}
    check("D4 packages that gave up are reported, not claimed",
          len(exhausted_pkgs) == 0 or int(counters.get("exhausted", 0)) + int(counters.get("backpressure", 0)) > 0,
          f"{len(exhausted_pkgs)} faulted packages never accepted; scheduler counters {counters}")
    # D5 camera health during faults
    not_healthy = [(round(t - phase_marks[0][1]), m, s) for t, m, s in camera_samples
                   if m != "ok" and any(v != "healthy" for v in s.values())]
    check("D5 cameras stay healthy while delivery fails", not not_healthy and len(camera_samples) > 0,
          f"{len(camera_samples)} samples; not healthy during faults: {not_healthy[:4]}")
    # D6 resume, no conflicts / invalid, no fatal
    last_ok_start = phase_marks[-1][1]
    accepted_after = [a for a in attempts if a["t"] >= last_ok_start + 10 and a["status"] in (200, 201)]
    conflicts = [a for a in attempts if a["status"] == 409]
    invalid = [a for a in attempts if a.get("invalid")]
    fatal = re.findall(r"FATAL.*|Traceback.*", container_log)
    check("D6 delivery resumes; no conflicts, invalid packages or fatal errors",
          accepted_after and not conflicts and not invalid and not fatal,
          f"{len(accepted_after)} accepted in the last ok phase; 409s {len(conflicts)}; invalid {len(invalid)}; fatal {fatal[:2]}")
    per_mode = {}
    for a in attempts:
        per_mode.setdefault(a["mode"], {}).setdefault(str(a["status"]), 0)
        per_mode[a["mode"]][str(a["status"])] += 1
    (OUT / "results.json").write_text(json.dumps({"image": IMAGE, "results": results, "attempts_by_mode": per_mode,
                                                  "scheduler_counters": counters}, indent=1))
    log(f"done: {sum(r['passed'] for r in results)}/{len(results)} passed; attempts by mode {per_mode}")


if __name__ == "__main__":
    main()
