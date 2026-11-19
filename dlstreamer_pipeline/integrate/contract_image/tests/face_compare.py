#!/usr/bin/env python3
"""Face output of two runs of face_collect.py (same video, same desired state, same duration): how many face vectors
were sent, how many people (person presences) got at least one face, and faces per person.
Usage: face_compare.py <label>=<run dir> [<label>=<run dir> ...]"""
import json
import sys
from pathlib import Path

rows = []
for arg in sys.argv[1:]:
    label, run = arg.split("=", 1)
    packages = [json.loads(l) for l in (Path(run) / "packages.jsonl").read_text().splitlines() if l.strip()]
    people = {p["presence_id"] for p in packages if p.get("event_type") == "object_present" and p.get("presence_id")
              and (p.get("object_type") in (None, "person"))}
    feats = [p for p in packages if p.get("event_type") == "person_feature_sample"]
    faces = [p for p in feats if any("adaface" in e for e in p.get("embeddings") or [])]
    with_face = {p["presence_id"] for p in faces}
    body = [p for p in feats if any("transreid" in e for e in p.get("embeddings") or [])]
    rows.append({"run": label, "packages": len(packages), "person_presences": len(people),
                 "face_vectors_sent": len(faces), "presences_with_a_face": len(with_face & people) if people else len(with_face),
                 "share_of_people_with_a_face": round(len(with_face & people) / len(people), 3) if people else None,
                 "faces_per_person_with_face": round(len(faces) / len(with_face), 2) if with_face else 0,
                 "body_vectors_sent": len(body)})
for r in rows:
    print(json.dumps(r))
