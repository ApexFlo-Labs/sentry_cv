#!/usr/bin/env python3
"""A new desired-state revision must touch only the cameras it changes (user decision 2026-11-19).

One running container, 3 cameras (tcp replay sources), then:
  R2 zone geometry of camera 1 changed      -> applied live everywhere: no worker restart, no event-process restart,
                                               no camera removed or unavailable; packages carry revision 2
  R3 fps of camera 2 changed (10 -> 5)      -> only camera 2's source rebuilt + its event process restarted;
                                               cameras 1 and 3 untouched (same pids, never unavailable)
  R4 camera 3 drops the gait profile         -> only camera 3's event process restarted; no source rebuilt
  R5 one revision changes camera 1 (zone) AND camera 2 (fps) -> both applied together; camera 3 untouched
  R6 invalid revision (not increasing)       -> rejected; nothing restarted; previous revision keeps running
  R7 camera 4 added / R8 camera 1 removed / R9 camera 5 added (reuses the idle branch) / R10 remove + add + zone in
     one revision -> all live: same worker, no other camera restarted, rebuilt or unhealthy; removed cameras gone from
     /readyz, /healthz and packages; added cameras produce packages with the new revision
Evidence: worker pid (integrate9 in the container), event-process pid per camera (its own log line), engine log lines
"removed (...)" / "camera_status unavailable", /readyz per-camera state sampled every 2 s, packages' config_revision.
Usage: revision_per_camera_test.py <image> <desired_state.json (2 cameras, SA)> <fixture.mp4> <outdir>
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

IMAGE, BASE_STATE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
REPO = Path.home() / "Documents/sentry_cv"
PORT, SRC = int(os.getenv("TEST_API_PORT", "18495")), (19851, 19852, 19853, 19854, 19855)  # TEST_API_PORT: run next to camera_types_test.py
NAME = "dlsint-revision-per-camera"
OBSERVE = 75
results, samples, stop_sampling = [], [], threading.Event()


def log(msg):
    line = f"{time.strftime('%T')} {msg}"
    print(line, flush=True)
    with open(OUT / "revision_per_camera.log", "a") as f:
        f.write(line + "\n")


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def podman(*a):
    return subprocess.run(["podman", *a], capture_output=True, text=True)


def logs(since=None):
    r = podman("logs", *(["--since", since] if since else []), NAME)
    return r.stdout + r.stderr


KNOWN = "lazy pixels: frame no longer held"  # her code, first seconds after a worker start (RESULTS open item 1)


def worker_pid():
    code = ("import os\nfor p in os.listdir('/proc'):\n if p.isdigit():\n  try:\n"
            "   c=open(f'/proc/{p}/cmdline','rb').read()\n  except OSError:\n   continue\n"
            "  if b'integrate9.py' in c and b'multiprocessing' not in c: print(p)")
    return podman("exec", NAME, "python3", "-c", code).stdout.split()


def event_pids(text):
    pids = {}
    for m in re.finditer(r"(\S+): (?:event process ready \(pid|event process restarted \(pid) (\d+)", text):
        pids[m.group(1)] = m.group(2)
    return pids


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


def invalid_count():
    try:
        return (OUT / "agent.log").read_text(errors="replace").count("invalid_package")
    except OSError:
        return 0


def write_state(state):
    tmp = OUT / "config" / ".tmp.json"
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, OUT / "config" / "desired_state.json")


def sampler():
    while not stop_sampling.is_set():
        r = get("/readyz")
        samples.append((time.time(), {c["camera_id"]: (c["state"], c.get("reason")) for c in r.get("per_camera_state") or []}))
        time.sleep(2)


def wait_ready(revision, cameras, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h, r = get("/healthz"), get("/readyz")
        states = {c["camera_id"]: c["state"] for c in r.get("per_camera_state") or []}
        if h.get("revision") == revision and r.get("ready") and len(states) == cameras and all(s == "healthy" for s in states.values()):
            return time.time() - t0
        time.sleep(2)
    return None


def step(label, state, expect):
    """expect: {"worker_restart": bool, "event_restart": set, "source_rebuilt": set, "rejected": bool}"""
    w0, e0 = worker_pid(), event_pids(logs())
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    n0, inv0 = len(packages()), invalid_count()
    revision_before = get("/healthz").get("revision")
    t_write = time.time()
    write_state(state)
    revision = state["revision"]
    if expect.get("rejected"):
        time.sleep(20)
        h = get("/healthz")
        applied_s = None
    else:
        applied_s = wait_ready(revision, len(state["cameras"]))
    t_applied = time.time()
    time.sleep(OBSERVE)
    text = logs()
    window = logs(since)
    w1, e1 = worker_pid(), event_pids(text)
    cams = [c["camera_id"] for c in state["cameras"]]
    restarted = {c for c in cams if c in e0 and e1.get(c) != e0.get(c) and c not in expect.get("added", set())}
    removed = set(re.findall(r"(\S+): removed \(", window))
    seen_states = {}
    for t, st in samples:
        if t >= t_write:
            for c, v in st.items():
                if v[0] != "healthy":
                    seen_states.setdefault(c, set()).add(f"{v[0]}/{v[1]}")
    view_changed = expect.get("view_changed", set())  # V8: a changed zone/line -> that camera degraded/view_changed
    unavailable = {c for c, v in seen_states.items() if not (c in view_changed and v <= {"degraded/view_changed"})}
    all_errors = [l for l in window.splitlines() if re.search(r"FATAL|Traceback|event code error|reader error|settings not applied", l)]
    known = [l for l in all_errors if KNOWN in l]
    errors = [l for l in all_errors if KNOWN not in l]
    new = [p for p in packages()[n0:]]
    revs_after = sorted({p["config_revision"] for p in new[len(new) // 3:]} - {None}) if new else []
    per_cam_rev = {c: sorted({p.get("config_revision") for p in new[len(new) // 3:] if p.get("camera_id") == c}) for c in cams}
    invalid = invalid_count() - inv0
    checks = []
    if expect.get("rejected"):
        checks.append(("rejected, previous revision kept", h.get("revision") == revision_before and h.get("worker_running", True) is not False))
    else:
        checks.append(("applied, all cameras healthy", applied_s is not None))
    checks.append(("worker restart as expected", (w1 != w0) == bool(expect.get("worker_restart"))))
    if not expect.get("worker_restart"):
        checks.append((f"event processes restarted only {sorted(expect['event_restart'])}", restarted == expect["event_restart"]))
        checks.append((f"sources rebuilt only {sorted(expect['source_rebuilt'])}", removed == expect["source_rebuilt"]))
        allowed = expect["source_rebuilt"] | expect.get("added", set())
        checks.append(("no other camera unavailable", unavailable <= allowed))
    if not expect.get("rejected"):
        checks.append(("packages after the switch carry the new revision", revs_after == [revision]
                       and all(v == [revision] for v in per_cam_rev.values() if v)))
    gone = expect.get("removed", set())
    if gone:
        h_after, r_after = get("/healthz"), get("/readyz")
        listed = {c["camera_id"] for c in r_after.get("per_camera_state") or []}
        late_pkgs = [p for p in new if p.get("camera_id") in gone and p.get("accepted_at", "") >=
                     time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_applied + 10))]
        checks.append((f"removed {sorted(gone)}: not in /readyz, not in /healthz, no packages after", not (gone & listed)
                       and not any(g in json.dumps(h_after) for g in gone) and not late_pkgs))
    for a in expect.get("added", set()):
        checks.append((f"added {a}: produces packages with the new revision", per_cam_rev.get(a) == [revision]))
    checks.append(("no engine errors, no invalid packages", not errors and invalid == 0))
    ok = all(v for _, v in checks)
    results.append({"step": label, "revision": revision, "passed": ok, "checks": dict(checks),
                    "applied_seconds": None if applied_s is None else round(applied_s, 1),
                    "worker_pid": [w0, w1], "event_pids_before": e0, "event_pids_after": e1,
                    "event_restarted": sorted(restarted), "sources_removed": sorted(removed),
                    "not_healthy_in_window": {c: sorted(v) for c, v in seen_states.items()}, "packages": len(new),
                    "known_warmup_messages": len(known),
                    "config_revisions_last_two_thirds": revs_after, "per_camera_revisions": per_cam_rev,
                    "errors": errors[:5], "invalid_packages": invalid})
    log(f"{'PASS' if ok else 'FAIL'} {label}: applied in {applied_s and round(applied_s)} s; worker {w0}->{w1}; "
        f"event restarted {sorted(restarted)}; sources rebuilt {sorted(removed)}; not healthy {sorted(unavailable)}; "
        f"{len(new)} packages, revisions {revs_after}; errors {len(errors)} (+{len(known)} known warm-up); invalid {invalid}; "
        f"states {({c: sorted(v) for c, v in seen_states.items()})}")
    for name, v in checks:
        log(f"   {'ok ' if v else 'BAD'} {name}")
    for e in errors[:5]:
        log("   " + e[:250])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for d in ("config", "secrets", "delivery"):
        (OUT / d).mkdir(exist_ok=True)
    base = json.loads(BASE_STATE.read_text())
    proto = base["cameras"][0]
    cams = []
    for i in range(5):
        c = json.loads(json.dumps(proto))
        c["camera_id"] = f"rev-cam-{i + 1}"
        c["source"] = f"file:/run/secrets/sentinel/cameras/rev-cam-{i + 1}.url"
        ids = {b["use_case_id"]: f"{b['use_case_id'].rsplit('-', 1)[0]}-c{i + 1}" for b in c["bindings"]}  # unique per deployment
        for b in c["bindings"]:
            b["use_case_id"] = ids[b["use_case_id"]]
            for k, v in list(b.items()):
                if k.endswith("use_case_ids"):
                    b[k] = [ids.get(x, x) for x in v]
        cams.append(c)
    procs = []
    for c, port in zip(cams, SRC):
        (OUT / "secrets" / f"{c['camera_id']}.url").write_text(f"tcp://127.0.0.1:{port}\n")
        procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                       "--fixture", FIXTURE, "--port", str(port)],
                                      stdout=open(OUT / f"source-{port}.log", "wb"), stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/v8_capture_agent.py"),
                                   "--socket", str(OUT / "delivery/agent.sock"), "--schemas", str(REPO / "newdetails/sentry-v8"),
                                   "--log", str(OUT / "packages.jsonl"), "--status", str(OUT / "agent-status.json")],
                                  stdout=open(OUT / "agent.log", "wb"), stderr=subprocess.STDOUT))

    def state(revision, cameras):
        s = json.loads(json.dumps(base))
        s["revision"], s["cameras"] = revision, json.loads(json.dumps(cameras))
        return s

    s1 = state(1, cams[:3])
    write_state(s1)
    time.sleep(3)
    podman("rm", "-f", NAME)
    run = ["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false",
           "--tmpfs", "/dev/shm:rw,size=64m,mode=1777", "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777",
           "--tmpfs", "/plans:rw,size=64m,mode=1777", "--device", "/dev/dri", "--device", "/dev/accel/accel0",
           "--group-add", "keep-groups", "-e", f"APEX_API_PORT={PORT}",
           "-v", f"{OUT / 'config'}:/configs:ro", "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro",
           "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery", IMAGE]
    threading.Thread(target=sampler, daemon=True).start()
    try:
        subprocess.run(run, check=True, capture_output=True)
        t = wait_ready(1, 3)
        log(f"revision 1 ready after {t and round(t)} s (3 cameras); warm-up 60 s")
        time.sleep(60)
        c2 = json.loads(json.dumps(cams[:3]))
        c2[0]["geometry"]["zones"][0]["poly"] = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
        c2[0]["geometry"]["zones"][0]["name"] = "Inner"
        step("R2 zone of camera 1 changed", state(2, c2), {"event_restart": set(), "source_rebuilt": set(), "view_changed": {"rev-cam-1"}})
        c3 = json.loads(json.dumps(c2))
        c3[1]["fps"] = 5
        step("R3 fps of camera 2 changed", state(3, c3), {"event_restart": {"rev-cam-2"}, "source_rebuilt": {"rev-cam-2"}})
        c4 = json.loads(json.dumps(c3))
        for b in c4[2]["bindings"]:
            if b.get("embedding_profile_ids"):
                b["embedding_profile_ids"] = [p for p in b["embedding_profile_ids"] if p != "gaitbase-v18.1"]
        step("R4 camera 3 drops the gait profile", state(4, c4), {"event_restart": {"rev-cam-3"}, "source_rebuilt": set()})
        c5 = json.loads(json.dumps(c4))  # one revision, two cameras changed together; camera 3 unchanged
        c5[0]["geometry"]["zones"][0]["poly"] = [[0.01, 0.01], [0.99, 0.01], [0.99, 0.99], [0.01, 0.99]]
        c5[0]["geometry"]["zones"][0]["name"] = "Full again"
        c5[1]["fps"] = 10
        step("R5 one revision: zone of camera 1 + fps of camera 2", state(5, c5),
             {"event_restart": {"rev-cam-2"}, "source_rebuilt": {"rev-cam-2"}, "view_changed": {"rev-cam-1"}})
        step("R6 invalid revision (not increasing)", state(4, c5),
             {"event_restart": set(), "source_rebuilt": set(), "rejected": True})
        c7 = json.loads(json.dumps(c5)) + [json.loads(json.dumps(cams[3]))]
        step("R7 camera 4 added live", state(6, c7), {"event_restart": set(), "source_rebuilt": set(), "added": {"rev-cam-4"}})
        c8 = [c for c in json.loads(json.dumps(c7)) if c["camera_id"] != "rev-cam-1"]
        step("R8 camera 1 removed live", state(7, c8), {"event_restart": set(), "source_rebuilt": set(), "removed": {"rev-cam-1"}})
        c9 = json.loads(json.dumps(c8)) + [json.loads(json.dumps(cams[4]))]
        step("R9 camera 5 added (reuses camera 1's idle branch)", state(8, c9),
             {"event_restart": set(), "source_rebuilt": set(), "added": {"rev-cam-5"}})
        c10 = [c for c in json.loads(json.dumps(c9)) if c["camera_id"] != "rev-cam-3"] + [json.loads(json.dumps(cams[0]))]
        for c in c10:
            if c["camera_id"] == "rev-cam-2":
                c["geometry"]["zones"][0]["poly"] = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
        step("R10 one revision: camera 3 removed + camera 1 added back + zone of camera 2", state(9, c10),
             {"event_restart": set(), "source_rebuilt": set(), "removed": {"rev-cam-3"}, "added": {"rev-cam-1"},
              "view_changed": {"rev-cam-2"}})
    finally:
        stop_sampling.set()
        (OUT / "container.log").write_text(logs())
        podman("rm", "-f", NAME)
        for p in procs:
            p.terminate()
        (OUT / "results.json").write_text(json.dumps({"image": IMAGE, "results": results}, indent=1))
        log(f"done: {sum(r['passed'] for r in results)}/{len(results)} steps passed")


if __name__ == "__main__":
    main()
