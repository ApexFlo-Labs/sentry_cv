#!/usr/bin/env python3
"""Camera source types the desired state allows (rtsp, rtsps, http, https, tcp), on one running container.
  cam-https  https://  http_replay_source.py with a self-signed certificate

  cam-rtsp   rtsp://   mediamtx (plain)
  cam-rtsps  rtsps://  mediamtx with a self-signed certificate (what most cameras ship)
  cam-http   http://   sentry_cv http_replay_source.py (MPEG-TS over HTTP)
  cam-tcp    tcp://    sentry_cv reconnecting_replay.py
Checks: each camera healthy and producing packages; then each source is stopped for 30 s and must go
unavailable and come back without a restart (new stream session). Observations are logged for the semantic check.
Usage: camera_types_test.py <image> <desired_state.json (2 cams)> <fixture.mp4> <outdir>
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE, BASE, FIXTURE, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]).resolve()
REPO, HERE = Path.home() / "Documents/sentry_cv", Path(__file__).resolve().parent
MTX = Path.home() / "rtsp-fallback/mediamtx"
PORT, NAME = 18495, "dlsint-camera-types"
RTSP, RTSPS, HTTP, TCP, HTTPS = 18655, 18656, 19851, 19852, 19853
results = []


def log(m):
    line = f"{time.strftime('%T')} {m}"
    print(line, flush=True)
    with open(OUT / "camera_types.log", "a") as f:
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


def states():
    return {c["camera_id"]: c for c in get("/readyz").get("per_camera_state") or []}


def packages_by_camera():
    n = {}
    try:
        for line in (OUT / "packages.jsonl").read_text().splitlines():
            c = json.loads(line).get("camera_id")
            n[c] = n.get(c, 0) + 1
    except OSError:
        pass
    return n


def wait(fn, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(2)
    return None


def build_state(base):
    template = base["cameras"][0]
    tid = template["camera_id"]
    cams = []
    for cid in ("cam-rtsp", "cam-rtsps", "cam-http", "cam-tcp", "cam-https"):
        cam = json.loads(json.dumps(template).replace(tid, cid))
        geo = cam.get("geometry") or {}
        gmap = {g["id"]: f"{cid}-{g['id']}" for key in ("zones", "lines", "counting_lines") for g in geo.get(key) or []}
        umap = {b["use_case_id"]: f"{cid}-{b['use_case_id']}" for b in cam.get("bindings") or []}
        for key in ("zones", "lines", "counting_lines"):
            for g in geo.get(key) or []:
                g["id"] = gmap[g["id"]]
        for b in cam.get("bindings") or []:
            b["use_case_id"] = umap[b["use_case_id"]]
            b["geometry_ids"] = [gmap.get(x, x) for x in b.get("geometry_ids") or []]
            if "scene_trigger_use_case_ids" in b:
                b["scene_trigger_use_case_ids"] = [umap.get(x, x) for x in b["scene_trigger_use_case_ids"]]
        cams.append(cam)
    s = dict(base, cameras=cams, revision=1)
    return s


def main():
    for d in ("config", "secrets", "delivery", "mtx"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    state = build_state(json.loads(BASE.read_text()))
    (OUT / "config/desired_state.json").write_text(json.dumps(state, indent=1))
    urls = {"cam-rtsp": f"rtsp://127.0.0.1:{RTSP}/cam1", "cam-rtsps": f"rtsps://127.0.0.1:{RTSPS}/cam2",
            "cam-http": f"http://127.0.0.1:{HTTP}/live.ts", "cam-tcp": f"tcp://127.0.0.1:{TCP}",
            "cam-https": f"https://127.0.0.1:{HTTPS}/live.ts"}
    for cid, url in urls.items():
        (OUT / "secrets" / f"{cid}.url").write_text(url + "\n")
    key, crt = OUT / "mtx/server.key", OUT / "mtx/server.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "2", "-subj", "/CN=camera.local"], check=True, capture_output=True)
    (OUT / "mtx/mediamtx.yml").write_text(f"""logLevel: warn
api: false
metrics: false
pprof: false
playback: false
rtmp: false
hls: false
webrtc: false
srt: false
rtsp: true
rtspTransports: [tcp]
rtspEncryption: optional
rtspAddress: :{RTSP}
rtspsAddress: :{RTSPS}
rtspServerKey: {key}
rtspServerCert: {crt}
paths:
  all_others:
""")
    procs = {}

    def start(name):
        if name == "mtx":
            procs[name] = subprocess.Popen([str(MTX), str(OUT / "mtx/mediamtx.yml")], stdout=open(OUT / "mtx.log", "ab"),
                                           stderr=subprocess.STDOUT)
        elif name in ("pub1", "pub2"):
            path = "cam1" if name == "pub1" else "cam2"
            procs[name] = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", FIXTURE,
                                            "-an", "-c:v", "copy", "-f", "rtsp", "-rtsp_transport", "tcp",
                                            f"rtsp://127.0.0.1:{RTSP}/{path}"], stdout=open(OUT / f"{name}.log", "ab"),
                                           stderr=subprocess.STDOUT)
        elif name == "http":
            procs[name] = subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/http_replay_source.py"), "--fixture",
                                            FIXTURE, "--output", str(OUT / f"http-source-{time.time_ns()}"), "--port", str(HTTP), "--seconds", "3600"],
                                           stdout=open(OUT / "http.log", "ab"), stderr=subprocess.STDOUT)
        elif name == "https":
            procs[name] = subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/http_replay_source.py"), "--fixture",
                                            FIXTURE, "--output", str(OUT / f"https-source-{time.time_ns()}"), "--port", str(HTTPS),
                                            "--seconds", "3600", "--certificate", str(crt), "--key", str(key)],
                                           stdout=open(OUT / "https.log", "ab"), stderr=subprocess.STDOUT)
        elif name == "tcp":
            procs[name] = subprocess.Popen(["python3", str(REPO / "scripts/isolated_validation/reconnecting_replay.py"), "--fixture",
                                            FIXTURE, "--port", str(TCP)], stdout=open(OUT / "tcp.log", "ab"), stderr=subprocess.STDOUT)
        elif name == "agent":
            procs[name] = subprocess.Popen(["python3", str(HERE / "fault_agent.py"), "--socket", str(OUT / "delivery/agent.sock"),
                                            "--schemas", str(REPO / "newdetails/sentry-v8"), "--log", str(OUT / "packages.jsonl"),
                                            "--status", str(OUT / "agent-status.json"), "--mode-file", str(OUT / "agent-mode"),
                                            "--attempts", str(OUT / "attempts.jsonl"), "--observations", str(OUT / "observations.jsonl")],
                                           stdout=open(OUT / "agent.log", "ab"), stderr=subprocess.STDOUT)

    def stop(name):
        p = procs.pop(name, None)
        if p:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    (OUT / "agent-mode").write_text("ok")
    for n in ("mtx", "agent", "http", "tcp", "https"):
        start(n)
    time.sleep(2)
    for n in ("pub1", "pub2"):
        start(n)
    time.sleep(5)
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    try:
        subprocess.run(["podman", "run", "-d", "--name", NAME, "--network=host", "--read-only", "--read-only-tmpfs=false", "--tmpfs", "/dev/shm:rw,size=64m,mode=1777",
                        "--tmpfs", "/tmp/sentinel:rw,size=4g,mode=1777", "--tmpfs", "/plans:rw,size=64m,mode=1777",
                        "--device", "/dev/dri", "--device", "/dev/accel/accel0", "--group-add", "keep-groups",
                        "-e", f"APEX_API_PORT={PORT}", "-v", f"{OUT / 'config'}:/configs:ro",
                        "-v", f"{OUT / 'secrets'}:/run/secrets/sentinel/cameras:ro", "-v", f"{OUT / 'delivery'}:/run/sentinel-delivery",
                        IMAGE], check=True, capture_output=True)
        wait(lambda: get("/readyz").get("ready"), 900)
        time.sleep(90)
        st, pk = states(), packages_by_camera()
        for cid in urls:
            c = st.get(cid, {})
            check(f"T1 {cid} runs", c.get("state") == "healthy" and pk.get(cid, 0) > 0,
                  f"state {c.get('state')}/{c.get('reason')}, {pk.get(cid, 0)} packages")
        # outage + rejoin per type (rtsps shares the mediamtx publisher pub2)
        for cid, procs_to_stop, restart in (("cam-rtsp", ["pub1"], ["pub1"]), ("cam-rtsps", ["pub2"], ["pub2"]),
                                            ("cam-http", ["http"], ["http"]), ("cam-https", ["https"], ["https"]),
                                            ("cam-tcp", ["tcp"], ["tcp"])):
            if st.get(cid, {}).get("state") != "healthy":
                check(f"T2 {cid} outage and rejoin", False, "camera was not healthy before the outage")
                continue
            session = st[cid].get("stream_session_id")
            for n in procs_to_stop:
                stop(n)
            down = wait(lambda: states().get(cid, {}).get("state") == "unavailable", 60)
            time.sleep(max(0, 30 - 0))
            for n in restart:
                start(n)
            back = wait(lambda: states().get(cid, {}).get("state") == "healthy" and
                        states().get(cid, {}).get("stream_session_id") != session, 120)
            others = {k: v.get("state") for k, v in states().items() if k != cid}
            check(f"T2 {cid} outage and rejoin", bool(down) and bool(back),
                  f"went unavailable={bool(down)}, back healthy with a new stream session={bool(back)}; other cameras {others}")
        (OUT / "healthz-final.json").write_text(json.dumps(get("/healthz"), indent=1))
    finally:
        (OUT / "container.log").write_text(subprocess.run(["podman", "logs", NAME], capture_output=True, text=True).stderr +
                                           subprocess.run(["podman", "logs", NAME], capture_output=True, text=True).stdout)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        for n in list(procs):
            stop(n)
        (OUT / "results.json").write_text(json.dumps({"image": IMAGE, "results": results}, indent=1))
        log(f"done: {sum(r['passed'] for r in results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
