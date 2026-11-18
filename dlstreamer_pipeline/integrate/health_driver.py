"""Checks /readyz, /healthz and /metrics against the contract while a camera drops and the engine stalls.
Usage: health_driver.py PORT T0_EPOCH DROP_AT DROP_FOR"""
import json, os, signal, sys, time, urllib.request
PORT, T0, DROP_AT, DROP_FOR = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
def get(path):
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=3); return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()
    except Exception as e: return None, str(e)
def j(path): c, b = get(path); return c, (json.loads(b) if c else {})
def at(t): time.sleep(max(0, T0 + t - time.time()))
def say(t): print(time.strftime("%T"), t, flush=True)
res = []
def check(name, ok, detail): res.append(ok); say(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
cam = lambda p, i: next(x for x in p["per_camera_state"] if x["camera_id"] == i)
at(DROP_AT - 25)
c, p = j("/readyz"); ch, h = j("/healthz"); cm, m = get("/metrics")
need = {"ready", "worker_running", "per_camera_state"}; camkeys = {"state", "reason", "last_reliable_at", "as_of", "retrying"}
check("A1 /readyz fields", c == 200 and need <= set(p) and all(camkeys <= set(x) for x in p["per_camera_state"]) and all(x["state"] == "healthy" for x in p["per_camera_state"]),
      f"{c}, keys {sorted(set(p))}, camera 0 {json.dumps(p['per_camera_state'][0])[:260]}")
cap = h.get("capacity", {})
check("A2 capacity reported, no pressure at 4 cameras", cap.get("requested_aggregate_fps") == 40 and cap.get("resolved_aggregate_fps") and abs(cap["resolved_aggregate_fps"] - 40) <= 2 and cap.get("runtime_pressure") == "none",
      json.dumps(cap))
names = ["apexfabric_runtime_ready 1", "apexfabric_runtime_cameras 4", "apexfabric_runtime_revision", "sentinel_camera_decoded_fps{", "sentinel_camera_inference_fps{",
         "sentinel_camera_last_frame_age_seconds{", "sentinel_camera_reconnects_total{", "sentinel_runtime_resolved_fps", 'state="healthy"} 1']
check("A3 /metrics", cm == 200 and all(n in m for n in names), f"{cm}, {len(m.splitlines())} lines, missing {[n for n in names if n not in m]}")
at(DROP_AT + 25)
c, p = j("/readyz"); x = cam(p, "cam01")
others = [y["state"] for y in p["per_camera_state"] if y["camera_id"] != "cam01"]
check("B camera lost -> unavailable, runtime stays ready", c == 200 and x["state"] == "unavailable" and x["reason"] == "stream_disconnected" and x["retrying"] and all(s == "healthy" for s in others),
      f"readyz {c}, cam01 {x['state']}/{x['reason']} last_reliable_at {x['last_reliable_at']}, others {others}")
at(DROP_AT + DROP_FOR + 40)
c, p = j("/readyz"); x = cam(p, "cam01")
check("C camera back -> healthy", c == 200 and x["state"] == "healthy" and x.get("reconnects", 0) >= 1, f"readyz {c}, cam01 {x['state']} reconnects {x.get('reconnects')} decoded {x.get('decoded_fps')} fps")
_, h = j("/healthz"); pid = h.get("engine_pid")
os.kill(pid, signal.SIGSTOP); time.sleep(8)
c, p = j("/readyz"); states = {(y["state"], y["reason"]) for y in p["per_camera_state"]}
os.kill(pid, signal.SIGCONT)
check("D1 engine silent -> not ready, cameras unavailable heartbeat_silence", c == 503 and states == {("unavailable", "heartbeat_silence")}, f"readyz {c}, states {states}")
time.sleep(12)
c, p = j("/readyz")
check("D2 engine resumes -> ready again", c == 200 and all(y["state"] == "healthy" for y in p["per_camera_state"]), f"readyz {c}, {[y['state'] for y in p['per_camera_state']]}")
say(f"SUMMARY {sum(res)}/{len(res)} passed")
