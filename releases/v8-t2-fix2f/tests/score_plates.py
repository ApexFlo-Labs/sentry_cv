#!/usr/bin/env python3
"""Plate A/B scorer (same definition as T2_DESIGN_REVIEW check 5).
usage: score_plates.py RUN_DIR [RUN_DIR ...]   (RUN_DIR holds packages.jsonl from the probe)
duplicate = a plate_read whose text was already read on a DIFFERENT presence within the previous 8 s."""
import json, sys, collections
from datetime import datetime

def t(p): return datetime.fromisoformat(p['observed_at'].replace('Z', '+00:00')).timestamp()

for d in sys.argv[1:]:
    L = [json.loads(l) for l in open(f'{d}/packages.jsonl')]
    ev = collections.Counter(p['event_type'] for p in L)
    reads = sorted((p for p in L if p['event_type'] == 'plate_read'), key=t)
    last = {}  # text -> list of (time, presence)
    dup = 0
    for p in reads:
        x, pr, tt = p['plate_text'], p['presence_id'], t(p)
        if any(pp != pr and tt - t0 <= 8 for t0, pp in last.get(x, [])): dup += 1
        last.setdefault(x, []).append((tt, pr))
    texts = collections.Counter(p['plate_text'] for p in reads)
    print(json.dumps(dict(run=d, plate_read=len(reads), distinct_texts=len(texts), duplicates_other_presence_8s=dup,
                          object_present=ev['object_present'], presence_recovered=ev['presence_recovered'],
                          track_lost=ev['track_lost'], plate_outcome=ev['plate_outcome'])))
