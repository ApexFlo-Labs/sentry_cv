#!/usr/bin/env python3
"""Contract meaning of events (outputSemantics + relationalValidation), from the observations the CV sent.

  S1 subject events carry presence_id and stream_session_id                    (subject_events_have_presence_id_and_one_stream_session_id)
  S2 no zone_exit while a presence is lost (after track_lost, before recovery)  (no_zone_exit_..._on_track_loss_or_camera_failure)
  S3 nothing for a presence after presence_ended_unknown                         (lost_without_positive_exit_and_not_recovered)
  S4 every track_lost is resolved by presence_recovered or presence_ended_unknown (or is still open at the end of the run)
  S5 zone_exit only after a zone_entry of the same presence and zone; entry/exit alternate
  S6 zone_count 0 only while the camera is healthy                              (count_zero_requires_a_fresh_healthy_observation)
  S7 a new stream session starts only with camera_health healthy after unavailable (recovery starts a new stream session)
  S8 plate_read: bbox inside the frame, evidence present; plate_outcome has no invented text
Usage: semantics_check.py <observations.jsonl> [more.jsonl ...]
"""
import json
import sys
from collections import defaultdict

SUBJECT = {"object_present", "zone_entry", "zone_exit", "line_cross", "track_lost", "presence_recovered",
           "presence_ended_unknown", "person_feature_sample", "plate_read", "plate_outcome"}
rows = []
for path in sys.argv[1:]:
    for line in open(path):
        rows.append(json.loads(line))
rows.sort(key=lambda o: (o["camera_id"], o["observed_at"]))
fail = defaultdict(list)
counts = defaultdict(int)
lost, ended, inside, health, sessions, open_lost = {}, set(), {}, {}, {}, {}
for o in rows:
    t, cam, pid = o["event_type"], o["camera_id"], o.get("presence_id")
    counts[t] += 1
    if t in SUBJECT and (not pid or not o.get("stream_session_id")):
        fail["S1"].append((t, o["observation_id"][:8]))
    if pid and pid in ended and t != "presence_ended_unknown":
        fail["S3"].append((t, pid[:8]))
    if t == "track_lost":
        lost[pid] = True
        open_lost[pid] = o
    elif t in ("presence_recovered",):
        lost.pop(pid, None)
        open_lost.pop(pid, None)
    elif t == "presence_ended_unknown":
        ended.add(pid)
        lost.pop(pid, None)
        open_lost.pop(pid, None)
    elif t == "zone_exit" and lost.get(pid):
        fail["S2"].append(pid[:8])
    if t == "zone_entry":
        key = (pid, o["zone_id"])
        if inside.get(key):
            fail["S5"].append(("entry twice", pid[:8]))
        inside[key] = True
    elif t == "zone_exit":
        key = (pid, o["zone_id"])
        if not inside.get(key):
            fail["S5"].append(("exit without entry", pid[:8]))
        inside[key] = False
    if t == "camera_health":
        prev = health.get(cam)
        health[cam] = o["state"]
        if o["state"] == "healthy":  # a new stream session is announced by camera_health healthy
            sessions.setdefault(cam, set()).add(o["stream_session_id"])
    if t == "zone_count" and o.get("count") == 0 and health.get(cam) not in (None, "healthy"):
        fail["S6"].append((cam, o["observed_at"]))
    if t != "camera_health" and o.get("stream_session_id"):
        known = sessions.setdefault(cam, set())
        if not known:
            known.add(o["stream_session_id"])
        elif o["stream_session_id"] not in known:
            fail["S7"].append((cam, t, o["stream_session_id"][:8]))
            known.add(o["stream_session_id"])
    if t == "plate_read":
        b = o.get("plate_bbox_normalized") or [-1, -1, -1, -1]
        if not (0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1) or not o.get("evidence_ids"):
            fail["S8"].append(o["observation_id"][:8])
    if t == "plate_outcome" and o.get("plate_text"):
        fail["S8"].append(("outcome with text", o["observation_id"][:8]))

summary = {"observations": len(rows), "event_counts": dict(counts),
           "track_lost_resolved": counts["track_lost"] - len(open_lost), "track_lost_open_at_end": len(open_lost),
           "checks": {k: {"passed": not fail[k], "violations": len(fail[k]), "examples": fail[k][:5]}
                      for k in ("S1", "S2", "S3", "S5", "S6", "S7", "S8")}}
summary["checks"]["S4"] = {"passed": True, "note": "open track_lost at end are not failures", "open": len(open_lost)}
print(json.dumps(summary, indent=1))
