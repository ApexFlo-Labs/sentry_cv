#!/usr/bin/env python3
"""Independent quiet/active/quiet counter and scene-pixel cadence oracle."""
import argparse
import json
import math
from pathlib import Path

import cv2


def motion_pattern(samples, camera_id, fps):
    rows=[]
    steady=[row for row in samples if row.get('sample_phase')=='steady']
    valid=True
    for first,last in zip(steady,steady[1:]):
        a=first.get('per_camera',{}).get(camera_id,{})
        b=last.get('per_camera',{}).get(camera_id,{})
        if (a.get('telemetry_fresh') is not True or b.get('telemetry_fresh') is not True
                or any(a.get(k)!=b.get(k) for k in ['stream_session_id','camera_process_pid'])):
            valid=False;continue
        try:
            duration=b['counter_uptime_seconds']-a['counter_uptime_seconds']
            inferred=b['totals'].get('inference_frames',0)-a['totals'].get('inference_frames',0)
            quiet=b['totals'].get('quiet_frames',0)-a['totals'].get('quiet_frames',0)
            if (not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
                        for v in [duration,inferred,quiet]) or duration<=0 or inferred<0 or quiet<0):
                valid=False;continue
        except (KeyError,TypeError):
            valid=False;continue
        i,q=inferred/duration,quiet/duration
        kind=('quiet' if i<=1 and q>=fps*.75 else 'active' if i>=fps*.8 else 'transition')
        rows.append({'kind':kind,'inference_fps':i,'quiet_fps':q,'duration':duration})
    pattern=[]
    for row in rows:
        if row['kind']!='transition' and (not pattern or pattern[-1]!=row['kind']):
            pattern.append(row['kind'])
    matched=any(pattern[n:n+3]==['quiet','active','quiet'] for n in range(max(0,len(pattern)-2)))
    return {'passed':valid and matched,'pattern':pattern,'windows':rows}


def analyze(raw):
    report=json.loads((raw/'report.json').read_text())
    first=next(row for row in report['samples'] if row.get('sample_phase')=='steady')
    cameras=list(first.get('per_camera',{}))
    assert len(cameras)==1,'Single-camera motion control expected'
    counter=motion_pattern(report['samples'],cameras[0],report['fps_per_camera'])
    packages={}
    for line in (raw/'packages.jsonl').read_text().splitlines():
        package=json.loads(line);packages.setdefault(package['package_id'],package)
    scenes=[]
    for metadata in (raw/'gallery').glob('*/metadata.json'):
        item=json.loads(metadata.read_text())
        if item.get('event_type')!='scene_sample':continue
        evidence=item.get('evidence_metadata') or []
        if not evidence:continue
        image=cv2.imread(str(metadata.parent/(evidence[0]['part_name']+'.jpg')),cv2.IMREAD_GRAYSCALE)
        if image is None:continue
        mean,std=float(image.mean()),float(image.std())
        kind='black' if mean<=2 and std<=2 else 'people' if mean>10 and std>10 else 'unknown'
        scenes.append({'package_id':item['package_id'],'kind':kind,'mean':mean,'std':std,
            'accepted_mono':packages[item['package_id']]['accepted_mono']})
    scenes.sort(key=lambda row:row['accepted_mono'])
    quiet_gaps=[];active_gaps=[]
    for a,b in zip(scenes,scenes[1:]):
        gap=b['accepted_mono']-a['accepted_mono']
        if a['kind']==b['kind']=='black' and 4<=gap<=7:quiet_gaps.append(gap)
        if a['kind']==b['kind']=='people' and 0<gap<=3:active_gaps.append(gap)
    cadence=len(quiet_gaps)>=3 and len(active_gaps)>=5
    return {'passed':bool(report['passed'] and counter['passed'] and cadence),
        'scope':__doc__+' Source is separately controlled black45/people60/black45 seconds, unmarked quiet pixels. '
            'Quiet completed frames remain healthy; five-second safety inference is allowed. '
            'Cadence bounds are this functional oracle, not a production gate override.',
        'image_config_id':report['image_config_id'],'counter_transition':counter,
        'quiet_scene_gaps_4_to_7_seconds':quiet_gaps,'active_scene_gaps_up_to_3_seconds':active_gaps,
        'scene_pixel_classification':scenes}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=analyze(args.raw)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'output':str(args.output)}))
    raise SystemExit(0 if result['passed'] else 1)
