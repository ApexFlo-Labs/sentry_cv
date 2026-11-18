"""Changes the desired state while the supervisor runs and checks each contract case. Usage: rt_driver.py DESIRED RUN_DIR PORT"""
import json, os, subprocess, sys, time, urllib.request, glob
D, R, PORT = sys.argv[1], sys.argv[2], sys.argv[3]
base = json.load(open(D))
def ready():
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/readyz", timeout=3); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
    except Exception as e: return None, {"error": str(e)}
def health():
    try: return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=3).read())
    except Exception as e: return {"error": str(e)}
def write(obj=None, raw=None):
    tmp = D + ".tmp"
    with open(tmp, "w") as f: f.write(raw if raw is not None else json.dumps(obj, indent=1))
    os.replace(tmp, D)
def status(): 
    try: return json.load(open(os.path.join(R, "state", "runtime", "engine_status.json")))
    except Exception: return {}
def log_count(text):
    try: return open(os.path.join(R, "integrate.log"), errors="ignore").read().count(text)
    except OSError: return 0
def say(t): print(time.strftime("%T"), t, flush=True)
results = []
def check(name, ok, detail):
    results.append((name, ok)); say(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
def wait_ready(timeout):
    t = time.time()
    while time.time() - t < timeout:
        c, p = ready()
        if c == 200: return c, p
        time.sleep(2)
    return ready()
c, p = wait_ready(400); h = health()
check("T0 start", c == 200 and p.get("active_revision") == base["revision"], f"readyz {c}, revision {p.get('active_revision')}, cameras {[x['state'] for x in p.get('per_camera_state', [])]}")
time.sleep(60); pid0 = health().get("engine_pid"); say(f"engine pid {pid0}")
# T1 hot: new zone on cam00, used by its presence binding
d = json.loads(json.dumps(base)); d["revision"] = base["revision"] + 1
cam = d["cameras"][0]; cam["geometry"]["zones"].append({"id": "cam00-test", "name": "Test zone", "poly": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]})
for b in cam["bindings"]:
    if b["app"] == "vehicle_presence": b["geometry_ids"].append("cam00-test")
write(d); time.sleep(10); c, p = ready(); h = health()
check("T1 hot reload (new zone)", c == 200 and p["active_revision"] == d["revision"] and h.get("engine_pid") == pid0 and log_count(f"hot config applied, revision {d['revision']}") == len(d["cameras"]),
      f"readyz {c} rev {p.get('active_revision')}, pid {h.get('engine_pid')} (was {pid0}), workers applied {log_count('hot config applied, revision ' + str(d['revision']))}/{len(d['cameras'])}")
rev_hot = d["revision"]; time.sleep(60)
# T2 invalid JSON
f0 = status().get("cameras", {}); write(raw="{ this is not json"); time.sleep(8); c, p = ready(); time.sleep(5); f1 = status().get("cameras", {})
flowing = all(f1.get(k, {}).get("frames_done", 0) > v.get("frames_done", 0) for k, v in f0.items())
check("T2 invalid file rejected, old one keeps running", c == 503 and p["reload_state"] == "rejected" and p["active_revision"] == rev_hot and health().get("engine_pid") == pid0 and flowing,
      f"readyz {c} {p.get('reload_state')} '{p.get('latest_error','')[:70]}', active {p.get('active_revision')}, frames still flowing {flowing}")
# T3 lower revision
d3 = json.loads(json.dumps(d)); d3["revision"] = base["revision"]; write(d3); time.sleep(8); c, p = ready()
check("T3 lower revision rejected", c == 503 and "must be greater" in p.get("latest_error", "") and p["active_revision"] == rev_hot and health().get("engine_pid") == pid0,
      f"readyz {c} '{p.get('latest_error','')[:80]}'")
# T4 valid again -> ready
d4 = json.loads(json.dumps(d)); d4["revision"] = rev_hot + 1; write(d4); time.sleep(10); c, p = ready()
check("T4 valid revision after rejects -> ready again", c == 200 and p["active_revision"] == d4["revision"] and health().get("engine_pid") == pid0, f"readyz {c} rev {p.get('active_revision')}")
# T5 fps change -> restart
d5 = json.loads(json.dumps(d4)); d5["revision"] = d4["revision"] + 1; d5["cameras"][1]["fps"] = 5; write(d5)
c, p = (None, {}); t = time.time()
while time.time() - t < 400:
    time.sleep(3); c, p = ready()
    if c == 200 and p.get("active_revision") == d5["revision"]: break
pid5 = health().get("engine_pid"); restart_s = time.time() - t
time.sleep(60); s1 = status()["cameras"]; time.sleep(30); s2 = status()["cameras"]
rate = {k: (s2[k]["frames_done"] - s1[k]["frames_done"]) / 30 for k in s2}
cam1 = d5["cameras"][1]["camera_id"]
check("T5 fps change -> engine restarted with new revision", c == 200 and pid5 not in (None, pid0) and 4.0 <= rate[cam1] <= 6.0,
      f"readyz {c} rev {p.get('active_revision')}, pid {pid0} -> {pid5}, ready after {restart_s:.0f} s, events/s per camera {rate}")
# T6 remove a camera -> restart
d6 = json.loads(json.dumps(d5)); d6["revision"] = d5["revision"] + 1; removed = d6["cameras"].pop()["camera_id"]; write(d6)
t = time.time()
while time.time() - t < 400:
    time.sleep(3); c, p = ready()
    if c == 200 and p.get("active_revision") == d6["revision"]: break
ids = [x["camera_id"] for x in p.get("per_camera_state", [])]
time.sleep(30)
check("T6 camera removed -> restart with fewer cameras", c == 200 and removed not in ids and len(ids) == len(d6["cameras"]) and set(status().get("cameras", {})) == set(ids),
      f"readyz {c} rev {p.get('active_revision')}, cameras {ids}")
pk = glob.glob(os.path.join(R, "packages", "*", "manifest.json"))
hits = sum("cam00-test" in open(x).read() for x in pk)
check("T1b new zone used by her V8 rules", hits > 0, f"{hits} of {len(pk)} saved packages mention zone cam00-test")
say(f"SUMMARY {sum(ok for _, ok in results)}/{len(results)} passed")
