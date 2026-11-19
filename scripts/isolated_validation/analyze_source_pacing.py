#!/usr/bin/env python3
"""Audit independent replay egress timestamps; never infer camera capture time."""
import argparse
import json
from pathlib import Path


def analyze(rows, start, end):
    selected=[r for r in rows if start<=r['first_bytes_submitted_mono']<=end]
    intervals=[]
    for a,b in zip(selected,selected[1:]):
        if (a['epoch'],a['producer_pid'])!=(b['epoch'],b['producer_pid']):
            continue
        ticks=(b['dts90k']-a['dts90k']+(1<<32))%(1<<33)-(1<<32)
        elapsed=b['first_bytes_submitted_mono']-a['first_bytes_submitted_mono']
        intervals.append({'actual_seconds':elapsed,'source_seconds':ticks/90000,
                          'ratio':elapsed/(ticks/90000) if ticks>0 else None})
    valid=[r for r in intervals if r['ratio'] is not None]
    return {'captured_frames':len(selected),'intervals_checked':len(intervals),
            'nonmonotonic_source_intervals':len(intervals)-len(valid),
            'minimum_wall_to_source_interval_ratio':min((r['ratio']for r in valid),default=None),
            'maximum_source_send_gap_seconds':max((r['actual_seconds']for r in valid),default=None),
            'burst_free':bool(valid and len(valid)==len(intervals) and all(r['ratio']>=.95 for r in valid)),
            'scope':'Test-source TCP submission times only; not decoder freshness, camera capture time or complete capacity qualification.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--start',type=float,required=True)
    p.add_argument('--end',type=float,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    reports={f.name:analyze([json.loads(s)for s in f.read_text().splitlines()],a.start,a.end)
             for f in sorted(a.directory.glob('*.frames.jsonl'))}
    result={'per_source':reports,'passed':bool(reports and all(r['burst_free']for r in reports.values()))}
    a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
