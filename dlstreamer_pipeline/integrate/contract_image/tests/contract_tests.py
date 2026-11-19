#!/usr/bin/env python3
"""Contract runtime checks the capacity probe does not cover, on the V8 deployment shape:
read-only root, uid 10001, writable /tmp/sentinel and /plans only, desired state as a mounted directory, camera URL
secrets, delivery-agent socket (sentry_cv's capture agent, schema-validating).

  C1 startup with one camera down (desiredState / runtimeAvailability.startup + cameraIsolation)
  C2 down camera comes up -> rejoins without restart (runtimeAvailability.recovery)
  C3 new valid revision -> validated, applied, events carry the new config_revision, old worker processes gone
  C4 lower revision -> rejected, last valid kept running, reported (desiredState.invalid)
  C5 schema-invalid revision -> rejected, last valid kept running
  C6 next valid revision after rejects -> applied
  C7 SIGTERM -> worker stops through the stop file, container exits
Usage: contract_tests.py <image> <desired_state.json from a probe run (2 cameras)> <fixture.mp4> <outdir>
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

IMAGE, BASE_STATE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
REPO = Path.home() / "Documents/sentry_cv"
PORT, GOOD, DEAD = 18491, 19811, 19812
NAME = "dlsint-contract-tests"
results = []


def log(msg):
    line = f"{time.strftime('%T')} {msg}"
    print(line, flush=True)
    with open(OUT / "contract_tests.log", "a") as f:
        f.write(line + "\n")


def check(case, ok, detail):
    results.append({"case": case, "passed": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'} {case}: {detail}")


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:  # /readyz answers 503 while not ready; its body still has the camera states
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def wait(what, fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        value = fn()
        if value:
            return value
        time.sleep(3)
    log(f"timeout waiting for {what}")
    return None


def podman(*args, check_rc=False):
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check_rc)


def container_log():
    return podman("logs", NAME).stdout + podman("logs", NAME).stderr


def write_state(state):
    tmp = OUT / "config" / ".desired_state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, OUT / "config" / "desired_state.json")


def packages():
    rows = []
    try:
        for line in (OUT / "packages.jsonl").read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


def camera_state(health, camera_id):
    for c in health.get("per_camera_state") or []:
        if c.get("camera_id") == camera_id:
            return c
    return {}


def processes():
    out = podman("top", NAME, "pid", "args").stdout.splitlines()[1:]
    return [line for line in out if "python3" in line]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for d in ("config", "secrets", "delivery"):
        (OUT / d).mkdir(exist_ok=True)
    state = json.loads(BASE_STATE.read_text())
    cams = [c["camera_id"] for c in state["cameras"]]
    assert len(cams) == 2, cams
    state["revision"] = 1
    (OUT / "secrets" / f"{cams[0]}.url").write_text(f"tcp://127.0.0.1:{GOOD}\n")
    (OUT / "secrets" / f"{cams[1]}.url").write_text(f"tcp://127.0.0.1:{DEAD}\n")  # nothing listens yet
    write_state(state)
    procs = []
    procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                   "--fixture", FIXTURE, "--port", str(GOOD)],
                                  stdout=open(OUT / "source-good.log", "wb"), stderr=subprocess.STDOUT))
    procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/v8_capture_agent.py"),
                                   "--socket", str(OUT / "delivery/agent.sock"), "--schemas", str(REPO / "newdetails/sentry-v8"),
                                   "--log", str(OUT / "packages.jsonl"), "--status", str(OUT / "agent-status.json")],
                                  stdout=open(OUT / "agent.log", "wb"), stderr=subprocess.STDOUT))
    time.sleep(3)
    podman("rm", "-f", NAME)
    run = ["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777",
           "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
           "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
           "-e", f"APEX_API_PORT={PORT}", "-e", "WORKER_CAMERA_INITIALIZATION_SECONDS=180",  # test only (image: 600)
           "-v", f"{OUT / 'config'}:/configs:ro", "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro",
           "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery", IMAGE]
    log("run: " + " ".join(run))
    try:
        subprocess.run(run, check=True, capture_output=True)
        user = podman("exec", NAME, "id", "-u").stdout.strip()
        ro = podman("exec", NAME, "sh", "-c", "touch /opt/x 2>/dev/null || echo root_ro; touch /tmp/x 2>/dev/null || echo tmp_ro; "
                    "touch /tmp/sentinel/x && echo sentinel_rw; touch /dev/shm/x && echo shm_rw").stdout
        flags = {k: k in ro for k in ("root_ro", "tmp_ro", "sentinel_rw", "shm_rw")}
        check("C0 deployment shape", user == "10001" and all(flags.values()), f"uid {user}; {flags}")
        # C1
        ready = wait("ready", lambda: (h := get("/readyz")).get("ready") and h, 900)
        h = get("/readyz") if not ready else ready
        c0, c1 = camera_state(h, cams[0]), camera_state(h, cams[1])
        fatal = "FATAL" in container_log()
        check("C1 startup with one camera down", bool(ready) and c0.get("state") == "healthy" and c1.get("state") != "healthy" and not fatal,
              f"ready={bool(ready)} {cams[0]}={c0.get('state')} {cams[1]}={c1.get('state')}/{c1.get('reason')} FATAL in log={fatal}")
        time.sleep(30)
        n0 = len(packages())
        alive = get("/healthz")
        check("C1b healthy camera keeps producing while the other is down",
              n0 > 0 and alive.get("ready") is not False, f"{n0} packages so far, healthz ready={alive.get('ready')}")
        # C2
        procs.append(subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                       "--fixture", FIXTURE, "--port", str(DEAD)],
                                      stdout=open(OUT / "source-late.log", "wb"), stderr=subprocess.STDOUT))
        pid_before = processes()
        joined = wait("late camera healthy", lambda: camera_state(get("/readyz"), cams[1]).get("state") == "healthy", 120)
        late_pkgs = wait("late camera packages", lambda: any(p.get("camera_id") == cams[1] for p in packages()), 60)
        check("C2 late camera joins without restart", bool(joined) and bool(late_pkgs) and processes() and
              set(p.split()[0] for p in pid_before) <= set(p.split()[0] for p in processes()),
              f"healthy={bool(joined)} packages={bool(late_pkgs)} same worker processes={set(p.split()[0] for p in pid_before) <= set(p.split()[0] for p in processes())}")
        # C3
        old_pids = {p.split()[0] for p in processes() if "integrate9.py" in p or "multiprocessing" in p}
        state2 = json.loads(json.dumps(state))
        state2["revision"] = 2
        zones = state2["cameras"][0].get("geometry", {}).get("zones") or []
        if zones:
            zones[0]["name"] = zones[0].get("name", "zone") + " r2"
        write_state(state2)
        t_switch = time.time()
        applied = wait("revision 2 applied", lambda: get("/healthz").get("revision") == 2, 900)
        time.sleep(40)
        after = [p for p in packages() if p.get("accepted_at", "") >= time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_switch + 30))]
        revs = sorted({p.get("config_revision") for p in after} - {None})
        new_pids = {p.split()[0] for p in processes() if "integrate9.py" in p or "multiprocessing" in p}
        check("C3 new revision applied live (zone change: same cameras -> no worker restart, 2026-11-19)", bool(applied) and old_pids <= new_pids and revs == [2],
              f"revision 2 active after {time.time() - t_switch:.0f} s; config_revision in latest packages {revs}; "
              f"worker processes kept (live apply) {sorted(old_pids & new_pids)}")
        # C4
        state_low = json.loads(json.dumps(state2))
        state_low["revision"] = 1
        write_state(state_low)
        time.sleep(15)
        h = get("/healthz")
        check("C4 lower revision rejected, last valid kept", h.get("revision") == 2 and bool(processes()),
              f"active revision {h.get('revision')}, worker running={bool(processes())}, readyz ready={get('/readyz').get('ready')}")
        # C5
        bad = json.loads(json.dumps(state2))
        bad["revision"] = 3
        bad["cameras"][0]["fps"] = "fast"
        write_state(bad)
        time.sleep(15)
        h = get("/healthz")
        check("C5 schema-invalid revision rejected", h.get("revision") == 2 and bool(processes()),
              f"active revision {h.get('revision')}, worker running={bool(processes())}")
        # C5b valid revision whose cameras are all unreachable (user decision 2026-11-19, option a): applied live; the
        # cameras are reported unavailable and keep retrying (camera health, not an invalid revision); no worker restart
        dead = json.loads(json.dumps(state2))
        dead["revision"] = 3
        for i, cam in enumerate(dead["cameras"]):
            cam["camera_id"] = f"dead-{i + 1:02d}"
            cam["source"] = f"file:/run/secrets/sentinel/cameras/dead-{i + 1:02d}.url"
            (OUT / "secrets" / f"dead-{i + 1:02d}.url").write_text(f"tcp://127.0.0.1:{19899 - i}\n")
        workers_before = {p.split()[0] for p in processes() if "integrate9.py" in p and "multiprocessing" not in p}
        write_state(dead)
        applied = wait("revision 3 applied", lambda: get("/healthz").get("revision") == 3, 120)
        time.sleep(40)
        r = get("/readyz")
        states = {c["camera_id"]: (c.get("state"), c.get("reason")) for c in r.get("per_camera_state") or []}
        late = [subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"),
                                  "--fixture", FIXTURE, "--port", str(19899 - i)],
                                 stdout=open(OUT / f"source-dead-{i + 1}.log", "wb"), stderr=subprocess.STDOUT)
                for i in range(len(dead["cameras"]))]
        procs.extend(late)
        retrying = wait("unreachable cameras rejoin once their sources start", lambda: all(
            camera_state(get("/readyz"), c["camera_id"]).get("state") == "healthy" for c in dead["cameras"]), 120)
        for p in late:
            p.terminate()
        workers_after = {p.split()[0] for p in processes() if "integrate9.py" in p and "multiprocessing" not in p}
        unavailable = bool(states) and set(states) == {c["camera_id"] for c in dead["cameras"]} and all(
            v[0] == "unavailable" for v in states.values())
        check("C5b all cameras unreachable -> revision applied, cameras unavailable and retrying, no restart",
              bool(applied) and unavailable and workers_before == workers_after and retrying,
              f"revision {get('/healthz').get('revision')}, cameras while unreachable {states}, rejoined when sources started={bool(retrying)}, "
              f"worker {workers_before}->{workers_after}")
        # C6
        state4 = json.loads(json.dumps(state2))
        state4["revision"] = 4
        write_state(state4)
        applied = wait("revision 4 applied", lambda: get("/healthz").get("revision") == 4 and get("/readyz").get("ready"), 900)
        check("C6 valid revision after rejects", bool(applied), f"revision 4 active and ready={bool(applied)}")
        # C8 one camera's event process dies -> only that camera restarts, others keep producing
        before = container_log()
        n_restart0, n_fatal0, n_ready0 = before.count("restarting it"), before.count("FATAL"), before.count("event process ready")
        n_before = {c: sum(1 for p in packages() if p.get("camera_id") == c) for c in cams}
        r = podman("exec", NAME, "python3", "-c",
                   "import glob,re\nfor d in glob.glob('/proc/[0-9]*'):\n try:\n  m=open(d+'/maps').read()\n except OSError: continue\n"
                   " rings=set(re.findall(r'psm_[0-9a-f]+', m))\n if len(rings)==1: print(d[6:])")
        candidates = r.stdout.split()  # event processes map exactly one camera ring; the engine maps all of them
        killed = None
        if candidates:
            killed = candidates[0]
        if killed:
            podman("exec", NAME, "kill", "-9", killed)
        time.sleep(60)
        after = container_log()
        restarted = after.count("restarting it") > n_restart0 and after.count("event process ready") > n_ready0
        fatal_new = after.count("FATAL") > n_fatal0
        n_after = {c: sum(1 for p in packages() if p.get("camera_id") == c) for c in cams}
        both_produce = all(n_after[c] > n_before[c] for c in cams)
        health_ok = all(camera_state(get("/readyz"), c).get("state") == "healthy" for c in cams)
        check("C8 one camera process dies -> only it restarts", bool(killed) and restarted and both_produce and health_ok
              and not fatal_new,
              f"killed pid {killed}; restart logged={restarted}; both cameras kept producing={both_produce} "
              f"({n_before} -> {n_after}); healthy={health_ok}; new FATAL={fatal_new}")
        # C9 the whole worker dies -> the image restarts it with the last valid revision
        r = podman("exec", NAME, "python3", "-c",
                   "import glob\nfor d in glob.glob('/proc/[0-9]*'):\n try:\n  a=open(d+'/cmdline','rb').read()\n except OSError: continue\n"
                   " if b'integrate9.py' in a and b'spawn_main' not in a: print(d[6:])")
        worker = r.stdout.split()
        restarts0 = container_log().count("worker_exited_restarting")
        if worker:
            podman("exec", NAME, "kill", "-9", worker[0])
        t_kill = time.time()
        back = wait("worker restarted and ready", lambda: container_log().count("worker_exited_restarting") > restarts0 and
                    container_log().count('"worker_started"') > 0 and get("/readyz").get("ready") and
                    get("/healthz").get("revision") == 4 and time.time() - t_kill > 5, 300)
        new_pid = [l for l in podman("exec", NAME, "python3", "-c",
                   "import glob\nfor d in glob.glob('/proc/[0-9]*'):\n try:\n  a=open(d+'/cmdline','rb').read()\n except OSError: continue\n"
                   " if b'integrate9.py' in a and b'spawn_main' not in a: print(d[6:])").stdout.split()]
        check("C9 worker crash -> image restarts it", bool(worker) and bool(back) and new_pid and new_pid != worker,
              f"killed worker pid {worker[:1]}; restart logged and ready again after {time.time() - t_kill:.0f} s={bool(back)}; "
              f"new worker pid {new_pid[:1]}; revision {get('/healthz').get('revision')}")
        (OUT / "healthz-final.json").write_text(json.dumps(get("/healthz"), indent=1))
        # C7
        t0 = time.time()
        podman("stop", "-t", "60", NAME)
        st = podman("inspect", NAME, "--format", "{{.State.ExitCode}}").stdout.strip()
        stopped = "stop requested by the supervisor" in container_log()
        check("C7 clean stop", stopped, f"exit code {st}, stop-file path used={stopped}, {time.time() - t0:.1f} s")
    finally:
        (OUT / "container.log").write_text(container_log())
        podman("rm", "-f", NAME)
        for p in procs:
            p.terminate()
        agent = {}
        try:
            agent = json.loads((OUT / "agent-status.json").read_text())
        except (OSError, ValueError):
            pass
        (OUT / "results.json").write_text(json.dumps({"image": IMAGE, "results": results, "agent_status": agent}, indent=1))
        log(f"done: {sum(r['passed'] for r in results)}/{len(results)} passed; agent status {json.dumps(agent)[:400]}")


if __name__ == "__main__":
    main()
