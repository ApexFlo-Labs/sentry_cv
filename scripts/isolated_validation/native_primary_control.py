#!/usr/bin/env python3
"""Diagnostic control: baked native primary graph, no Sentinel policy/evidence.

Not a production-capacity test. Keeps baked decoder, detector/proc, tracker,
request count and TCP feeder. Read-only native probes count selected outputs.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


def branch_description(i,args):
    consuming=args.consumer!='discard'
    return (
            f'appsrc name=source{i} is-live=true format=bytes block=false '
            'max-bytes=1048576 leaky-type=downstream '
            'caps="video/mpegts,systemstream=true,packetsize=188" ! '
            f'tsdemux ! h264parse ! vah264dec name=decoder{i} ! vapostproc ! '
            'video/x-raw(memory:VAMemory),format=NV12 ! '
            + (f'identity name=rate{i} ! 'if args.admission else '') +
            ('gvamotiondetect pixel-diff-threshold=15 motion-threshold=0.05 '
               'block-size=64 confirm-frames=1 min-persistence=1 max-miss=0 '
               'min-rel-area=0.0005 ! ' if args.motion else '') +
            (f'identity name=admission{i} ! 'if args.admission else '') +
            'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! '
            f'gvadetect name=det{i} '
            'model=/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml '
            'model-proc=/opt/traffic-pilot/config/vehicle_yolo26_model_proc.json '
            'device=GPU pre-process-backend=va-surface-sharing '
            f'threshold=0.30 nireq={args.nireq} batch-size=1 '
            'model-instance-id=sentinel-native-control-shared ! '
            'gvatrack tracking-type=zero-term ! ' +
            (f'appsink name=sink{i} sync=false async=false max-buffers={args.pending_capacity} drop=false '
             'wait-on-eos=false enable-last-sample=false' if consuming else
             'fakesink sync=false async=false'))


def inside(args):
    sys.path.insert(0, '/opt/traffic-pilot/services/worker')
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    from pipeline.dlstreamer_bridge import TcpMpegTsFeeder
    from pipeline.native_pad_progress import NativePadProgress
    Gst.init([])
    branches = []
    consuming = args.consumer != 'discard'
    branches=[branch_description(i,args)for i in range(args.cameras)]
    coordinator=None
    if args.coordinator:
        from pipeline.multi_camera_gst import MultiCameraGstPipeline
        coordinator=MultiCameraGstPipeline([f'control-{i+1}'for i in range(args.cameras)],
                                          startup_camera_count=1)
        graph=coordinator.pipeline
    else:
        graph = Gst.parse_launch(' '.join(branches))
    counters = NativePadProgress()
    feeders = []
    samples = []
    report = {'scope': __doc__, 'passed': False, 'camera_count': args.cameras,
              'source_fps': 10, 'nireq': args.nireq, 'samples': samples,
              'consumer':args.consumer,'scene_device':args.scene_device,
              'motion_element':args.motion,'production_coordinator':args.coordinator,
              'camera_telemetry':args.telemetry,'production_admission':args.admission,
              'target_fps':args.target_fps,'nonblocking_admission_experiment':args.nonblocking_admission,'pending_capacity':args.pending_capacity}
    consumer_stats={}
    consumer_errors=[]
    camera_telemetry={}
    if args.telemetry:
        os.environ['SENTINEL_CAMERA_GROUP_MODE']='true'
        from pipeline.runtime_telemetry import CameraTelemetry
        for i in range(args.cameras):
            camera_telemetry[i]=CameraTelemetry(f'control-{i+1}',target_fps=args.target_fps,
                devices={'vehicle_person':'GPU/DLStreamer'})
    admissions={};clocks={};rate_gates={}
    if args.admission:
        from pipeline.dlstreamer_bridge import DlsMotionPolicyGate,DlsPtsRateGate
        from pipeline.frame_time import DecodedFrameClock
        from pipeline.surface_stream import SurfaceSwitch
        if args.nonblocking_admission:
            from collections import deque
            def try_reserve(self,record):
                with self._capacity:
                    if self._closed or self._suspended:return False
                    if self.pending_admissions>=self.maximum_pending:
                        self.diagnostic_rejected=getattr(self,'diagnostic_rejected',0)+1
                        return False
                    self._records.setdefault(record.pts,deque()).append(record)
                    return True
            DlsMotionPolicyGate._reserve=try_reserve
        for i in range(args.cameras):
            clocks[i]=DecodedFrameClock()
            rate_gates[i]=DlsPtsRateGate(args.target_fps,int(Gst.SECOND))
            admissions[i]=DlsMotionPolicyGate(SurfaceSwitch(14,4,6,5),camera_telemetry[i],
                clocks[i],args.target_fps,int(Gst.SECOND),maximum_pending=args.pending_capacity)
    scene_extractors={}
    if args.consumer=='metadata-bgr-scene':
        from pipeline.dls_secondary import DlsSceneExtractor
        for i in range(args.cameras):
            extractor=DlsSceneExtractor('/models/scene/openvino/siglip2-base-patch16-224-static.xml',
                '/opt/traffic-pilot/config/scene_siglip_model_proc.json',args.scene_device,
                owner_id=f'control-{i+1}')
            extractor.warmup()
            scene_extractors[i]=extractor
    def consume(index):
        from collections import Counter
        from pipeline.dlstreamer_bridge import detections_from_sample
        from pipeline.surface_stream import VAMemorySelectiveBgrConverter, _map_bgr_any
        from stream_fleet_openvino import VEHICLE_CLASS_NAMES
        converter=None;scene=scene_extractors.get(index);last_pixels=None;last_scene=None
        stats={'frames':0,'detections':0,'bgr':0,'scene':0,'metadata_wall':0.,'metadata_cpu':0.}
        consumer_stats[str(index+1)]=stats
        try:
            sink=graph.get_by_name(f'sink{index}')
            while True:
                sample=sink.emit('try-pull-sample',Gst.SECOND)
                if sample is None:continue
                pts=int(sample.get_buffer().pts)
                if args.admission:admissions[index].take(pts)
                began=time.perf_counter();cpu=time.thread_time()
                detections=detections_from_sample(sample,VEHICLE_CLASS_NAMES)
                if args.motion and 'motion_tracking_relations' not in stats:
                    from gi.repository import GstAnalytics,GLib
                    relation=GstAnalytics.buffer_get_analytics_relation_meta(sample.get_buffer())
                    metadata=list(relation) if relation is not None else []
                    motions=[m for m in metadata if type(m)is GstAnalytics.ODMtd
                             and GLib.quark_to_string(m.get_obj_type())=='motion']
                    tracks=[m for m in metadata if type(m)is GstAnalytics.TrackingMtd]
                    stats['motion_tracking_relations']=sum(
                        relation.get_relation(m.id,t.id)!=GstAnalytics.RelTypes.NONE
                        for m in motions for t in tracks)
                stats['metadata_wall']+=time.perf_counter()-began
                stats['metadata_cpu']+=time.thread_time()-cpu
                stats['detections']+=len(detections)
                stats['frames']+=1
                if index in camera_telemetry:
                    if args.admission:camera_telemetry[index].inference_succeeded()
                    else:camera_telemetry[index].frame(inferred=True)
                scene_due=scene is not None and (last_scene is None or pts-last_scene>=2*Gst.SECOND)
                pixels_due=args.consumer!='metadata' and (last_pixels is None or pts-last_pixels>=Gst.SECOND or scene_due)
                if pixels_due:
                    if converter is None:converter=VAMemorySelectiveBgrConverter(sample.get_caps(),Counter(),label='control')
                    frame=_map_bgr_any(converter.convert(sample));last_pixels=pts;stats['bgr']+=1
                    if scene_due:
                        vector=scene.embed(frame)
                        assert vector.shape==(768,)
                        stats['scene']+=1;last_scene=pts
                if args.admission:admissions[index].observe_models(bool(detections),False,fully_evaluated=True)
        except Exception as exc:
            consumer_errors.append({'camera':index+1,'type':type(exc).__name__,'message':str(exc)})
    Path('/results/graph.txt').write_text(' '.join(branches))
    try:
        def attach_counters(i):
            for element, direction in [(f'decoder{i}','src'), (f'det{i}','sink'), (f'det{i}','src')]:
                counters.attach(graph.get_by_name(element).get_static_pad(direction),
                                f'{element}.{direction}')
            if args.admission:
                def stamp_decoded(pad,info):
                    b=info.get_buffer()
                    if b is not None:clocks[i].record_with_generation(int(b.pts),time.time())
                    return Gst.PadProbeReturn.OK
                graph.get_by_name(f'decoder{i}').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,stamp_decoded)
                graph.get_by_name(f'rate{i}').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER,rate_gates[i].probe)
                graph.get_by_name(f'admission{i}').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER,admissions[i].probe)
        if not coordinator:
            for i in range(args.cameras):attach_counters(i)
            assert graph.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        for i in range(args.cameras):
            if coordinator:
                branch=coordinator.create_branch(f'control-{i+1}',branches[i])
                attach_counters(i)
                assert branch.set_state(Gst.State.PLAYING)!=Gst.StateChangeReturn.FAILURE
            feeder = TcpMpegTsFeeder(graph.get_by_name(f'source{i}'), '127.0.0.1',
                args.port_base+i, camera_id=f'control-{i+1}', stream_session_id='control')
            feeder.start()
            feeders.append(feeder)
            if consuming:
                threading.Thread(target=consume,args=(i,),daemon=True,
                                 name=f'control-consumer-{i+1}').start()
            if coordinator:time.sleep(1)
        started = time.monotonic()
        next_startup_log=started
        steady = None
        prior = None
        while time.monotonic()-started < 210:
            now = time.monotonic()
            snapshot = counters.snapshot()
            if args.admission:
                for gate in admissions.values():gate.drain_quiet()
            if steady is None and now>=next_startup_log:
                print(json.dumps({'phase':'startup','output_counts':{
                    str(i+1):snapshot[f'det{i}.src'][0] for i in range(args.cameras)},
                    'consumer_frames':{k:v['frames'] for k,v in consumer_stats.items()}}),flush=True)
                next_startup_log=now+5
            if consumer_errors:raise RuntimeError(str(consumer_errors))
            if coordinator:
                error=None
                for camera in coordinator.camera_ids:
                    error=coordinator.pop_message(camera,Gst.MessageType.ERROR)
                    if error:break
            else:
                error = graph.get_bus().timed_pop_filtered(0,Gst.MessageType.ERROR)
            if error:
                err, debug = error.parse_error()
                raise RuntimeError(str(err)+' '+str(debug))
            if steady is None and all(snapshot[f'det{i}.src'][0]>=30*args.target_fps for i in range(args.cameras)):
                Path('/results/phase.json').write_text(json.dumps({'phase':'steady'}))
                time.sleep(1)  # Let host supervisor receive the phase transition.
                snapshot = counters.snapshot()
                now = time.monotonic()
                steady = now
                report['steady_started_monotonic']=now
                prior = (now,snapshot)
            if prior and now-prior[0]>=10:
                elapsed=now-prior[0]
                fps={str(i+1):(snapshot[f'det{i}.src'][0]-prior[1][f'det{i}.src'][0])/elapsed
                     for i in range(args.cameras)}
                samples.append({'monotonic':now,'interval_seconds':elapsed,
                                'fps':fps,'snapshot':snapshot})
                print(json.dumps({'aggregate_fps':sum(fps.values()),'minimum_fps':min(fps.values())}),flush=True)
                prior=(now,snapshot)
            if all(snapshot[f'det{i}.src'][0]>30 for i in range(args.cameras)):
                ages=[now-snapshot[f'det{i}.src'][1] for i in range(args.cameras)]
                if max(ages)>8:
                    report['stalled_snapshot']=snapshot
                    raise RuntimeError('native primary output stale more than8s')
            if steady is not None and now-steady>=args.seconds:
                report['passed']=len(samples)>=3 and all(
                    min(s['fps'].values())>=.9*args.target_fps for s in samples) and all(
                    sum(s['fps'][str(i+1)]*s['interval_seconds'] for s in samples)
                    /sum(s['interval_seconds']for s in samples)>=.95*args.target_fps for i in range(args.cameras))
                break
            time.sleep(.25)
        else:
            report['error']='startup/measurement deadline'
    except Exception as exc:
        report['error']=str(exc)
    report['final_snapshot']=counters.snapshot()
    report['consumer_stats']=consumer_stats
    report['consumer_errors']=consumer_errors
    report['admissions']={str(k+1):{'decoded':v.decoded_frames,'pending':v.pending_admissions,
                                  'state':v.state,'pre_admission_rejected':getattr(v,'diagnostic_rejected',0)}for k,v in admissions.items()}
    report['camera_telemetry_snapshots']={str(k+1):v.latest_snapshot for k,v in camera_telemetry.items()}
    Path('/results/graph.dot').write_text(Gst.debug_bin_to_dot_data(graph,Gst.DebugGraphDetails.ALL))
    Path('/results/report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':report['passed'],'error':report.get('error')}),flush=True)
    # A failed driver can block normal Gst teardown. This diagnostic exits its
    # own process after saving evidence; it is not a recovery qualification.
    if consuming or not report['passed']:
        # Consumer controls deliberately exclude normal-teardown qualification.
        # Do not stop appsink consumers while shared inference still pushes.
        os._exit(0 if report['passed'] else 3)
    for feeder in feeders:
        feeder.close()
    counters.close()
    graph.set_state(Gst.State.NULL)
    return 0


def host(args):
    if args.output.exists():
        raise ValueError('fresh output directory required')
    descriptions=[branch_description(i,args)for i in range(args.cameras)]
    args.output.mkdir(parents=True)
    args.output.chmod(0o777)
    (args.output/'host-graph-preflight.json').write_text(json.dumps(descriptions,indent=2))
    script_copy=args.output/'control-script-used.py'
    script_copy.write_bytes(Path(__file__).read_bytes())
    identity=json.loads(subprocess.check_output(['podman','image','inspect',args.image],text=True))[0]
    (args.output/'image.json').write_text(json.dumps({'config_id':identity['Id']}))
    source=Path(__file__).with_name('reconnecting_replay.py')
    source_copy=args.output/'reconnecting-replay-used.py'
    source_copy.write_bytes(source.read_bytes())
    (args.output/'mpegts_pacing.py').write_bytes(source.with_name('mpegts_pacing.py').read_bytes())
    children=[]
    handles=[]
    try:
        for i in range(args.cameras):
            f=(args.output/f'source-{i+1}.log').open('w');handles.append(f)
            children.append(subprocess.Popen([sys.executable,str(source_copy),'--fixture',str(args.fixture),
                '--port',str(args.port_base+i),'--frame-log',str(args.output/f'source-{i+1}.frames.jsonl')],stdout=f,stderr=f))
        time.sleep(1)
        assert all(p.poll() is None for p in children)
        phase=os.environ.get('SENTINEL_VALIDATION_PHASE_FILE')
        if phase:Path(phase).write_text(json.dumps({'phase':'startup'}))
        command=['podman','run','--rm','--network','host','--device','/dev/dri/renderD128',
                 '--group-add','keep-groups','--entrypoint','python3']
        for key,label in [('SENTINEL_VALIDATION_RUN_ID','sentinel.validation_run'),
                          ('SENTINEL_VALIDATION_CASE','sentinel.validation_case')]:
            command+=['--label',label+'='+os.environ[key]]
        command+=['-v',f'{script_copy.resolve()}:/checks/control.py:ro',
                  '-v',f'{args.output.resolve()}:/results',identity['Id'],'/checks/control.py',
                  '--inside-image','--cameras',str(args.cameras),'--seconds',str(args.seconds),
                  '--nireq',str(args.nireq),'--port-base',str(args.port_base),
                  '--consumer',args.consumer,'--scene-device',args.scene_device]
        command+=['--target-fps',str(args.target_fps),'--pending-capacity',str(args.pending_capacity)]
        if args.motion:command+=['--motion']
        if args.coordinator:command+=['--coordinator']
        if args.telemetry:command+=['--telemetry']
        if args.admission:command+=['--admission']
        if args.nonblocking_admission:command+=['--nonblocking-admission']
        with (args.output/'container.log').open('w') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+270
            try:
                forwarded=False
                while process.poll() is None:
                    if phase and not forwarded and (args.output/'phase.json').is_file():
                        Path(phase).write_text((args.output/'phase.json').read_text())
                        forwarded=True
                    if time.monotonic()>deadline:raise TimeoutError('bounded native control deadline')
                    time.sleep(.2)
                return process.returncode
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
    finally:
        for p in children:
            if p.poll() is None:p.terminate()
        for p in children:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for f in handles:f.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside-image',action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--fixture',type=Path)
    parser.add_argument('--cameras',type=int,default=20)
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--port-base',type=int,default=19601)
    parser.add_argument('--nireq',type=int,default=8)
    parser.add_argument('--consumer',choices=['discard','metadata','metadata-bgr','metadata-bgr-scene'],default='discard')
    parser.add_argument('--scene-device',choices=['GPU','NPU'],default='GPU')
    parser.add_argument('--motion',action='store_true')
    parser.add_argument('--coordinator',action='store_true')
    parser.add_argument('--telemetry',action='store_true')
    parser.add_argument('--admission',action='store_true')
    parser.add_argument('--target-fps',type=float,default=10)
    parser.add_argument('--nonblocking-admission',action='store_true')
    parser.add_argument('--pending-capacity',type=int,choices=[2,3,4],default=2)
    args=parser.parse_args()
    if not 1<=args.cameras<=20:parser.error('bounded control supports1–20 cameras')
    if args.admission and (not args.motion or not args.telemetry or args.consumer=='discard'):
        parser.error('admission requires motion, telemetry and an appsink consumer')
    if args.target_fps!=10 and not args.admission:parser.error('FPS selection requires admission mode')
    if args.nonblocking_admission and not args.admission:parser.error('nonblocking experiment requires admission mode')
    raise SystemExit(inside(args) if args.inside_image else host(args))
