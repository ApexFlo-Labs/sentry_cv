"""Integrated FP32 raw fire-output diagnostic. Uses same model/input preprocessing,
but replaces GVA YOLO output conversion with raw tensors. Not a production
hazard/event/motion qualification or capacity run. No image is rebuilt.
"""
import hashlib,json,logging,threading,re
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.multi_camera_gst import MultiCameraGstPipeline
from pipeline.v8_events import V8EventPipeline
from pipeline.dlstreamer_bridge import DlsMotionPolicyGate

ROOT=Path('/run/sentinel-delivery/fire-path-diagnostic')
ROOT.mkdir(exist_ok=True)
raw_proc=json.loads(Path('/opt/traffic-pilot/config/fire_rgb_model_proc.json').read_text())
raw_proc['output_postproc']=[{'converter':'raw_data_copy'}]
raw_proc_path=ROOT/'fire_raw_proc.json';raw_proc_path.write_text(json.dumps(raw_proc))
lock=threading.Lock();saved=set()


def pixels(image):
    value=np.asarray(image)
    return {'shape':list(value.shape),'min':int(value.min()),'max':int(value.max()),
        'mean':float(value.mean()),'std':float(value.std()),
        'sha256':hashlib.sha256(value.tobytes()).hexdigest()}


def record(value):
    with lock:
        with (ROOT/'trace.jsonl').open('a')as f:f.write(json.dumps(value,separators=(',',':'))+'\n')


original=MultiCameraGstPipeline.create_branch

def create(self,camera_id,description,configure=None):
    def setup(branch):
        if configure:configure(branch)
        detector=branch.get_by_name('firedet')
        if detector is None:return
        from gi.repository import Gst,GstVideo
        from gstgva import VideoFrame
        properties={}
        for name in ['model','model-proc','device','ie-config','pre-process-backend',
                     'model-instance-id','nireq','batch-size','threshold','inference-region','object-class']:
            try:properties[name]=str(detector.get_property(name))
            except Exception as exc:properties[name]=type(exc).__name__
        record({'stage':'fire_element_properties','camera_id':camera_id,'properties':properties})
        for phase in ['sink','src']:
            def observe(pad,info,phase=phase):
                buffer=info.get_buffer();caps=pad.get_current_caps()
                if buffer is None or caps is None:return Gst.PadProbeReturn.OK
                try:
                    frame=VideoFrame(buffer,caps=caps)
                    with frame.data()as image:
                        copied=np.asarray(image).copy()
                    data=pixels(copied)
                    ok,mapped=buffer.map(Gst.MapFlags.READ)
                    if not ok:raise RuntimeError('Raw fire buffer map failed')
                    try:raw=bytes(mapped.data)
                    finally:buffer.unmap(mapped)
                    raw_sha=hashlib.sha256(raw).hexdigest()
                    meta=GstVideo.buffer_get_video_meta(buffer)
                    video_meta=({'width':meta.width,'height':meta.height,
                        'stride':list(meta.stride),'offset':list(meta.offset),
                        'format':str(meta.format)}if meta is not None else None)
                    with lock:
                        if data['sha256']not in saved and len(saved)<32:
                            np.save(ROOT/(data['sha256']+'.npy'),copied)
                            (ROOT/(data['sha256']+'.buffer')).write_bytes(raw)
                            saved.add(data['sha256'])
                    rois=[]
                    for roi in frame.regions():
                        rect=roi.rect();rois.append({'label':str(roi.label()),'confidence':float(roi.confidence()),
                            'bbox':[int(getattr(rect,k))for k in ['x','y','w','h']]})
                    tensor_records=[]
                    if phase=='src':
                        for tensor in frame.tensors():
                            value=tensor.data()
                            if value is None:continue
                            value=np.asarray(value)
                            item={'shape':list(value.shape),'dtype':str(value.dtype),'elements':int(value.size)}
                            if value.size==12600:
                                y=value.reshape(1,6,2100)
                                item.update(max_fire_score=float(y[0,4].max()),max_smoke_score=float(y[0,5].max()))
                                if data['min']==data['max']==0 and not (ROOT/'first_black_raw_output.npy').exists():
                                    np.save(ROOT/'first_black_raw_output.npy',value)
                            tensor_records.append(item)
                    record({'raw_tensors':tensor_records,'stage':'firedet.'+phase,'camera_id':camera_id,'pts_ns':int(buffer.pts),
                        'caps':caps.to_string(),'pixels':data,'rois':rois,'video_meta':video_meta,
                        'buffer_bytes':len(raw),'buffer_sha256':raw_sha,
                        'raw_nonzero_bytes':int(np.count_nonzero(np.frombuffer(raw,dtype=np.uint8)))})
                except Exception as exc:
                    record({'stage':'probe_error','phase':phase,'error':type(exc).__name__+': '+str(exc)})
                return Gst.PadProbeReturn.OK
            detector.get_static_pad(phase).add_probe(Gst.PadProbeType.BUFFER,observe)
    def raw_branch(match):
        fields=match.group(1)
        fields=re.sub(r'model-proc=(?:"[^"]+"|[^\s]+)',
                      'model-proc='+json.dumps(str(raw_proc_path)),fields)
        fields=re.sub(r'\bthreshold=[^\s]+\s*','',fields)
        fields=re.sub(r'ie-config=("[^"]+")',lambda v:'ie-config='+json.dumps(json.loads(v.group(1))+',INFERENCE_PRECISION_HINT=f32'),fields)
        return 'gvainference name=firedet '+fields+' ! appsink name=firesink'
    description,count=re.subn(r'gvadetect name=firedet (.*?) ! appsink name=firesink',raw_branch,description)
    if count!=1:raise RuntimeError('Expected one bounded native fire branch')
    return original(self,camera_id,description,configure=setup)
MultiCameraGstPipeline.create_branch=create

old_required=V8EventPipeline.hazard_pixels_required

def required(self,detections,now):
    result=old_required(self,detections,now)
    record({'stage':'hazard_pixels_required','camera_id':self.camera_id,'observed_at':now,
        'required':result,'policy':self.policy.section('fire_smoke'),
        'detections':[{'class':d.class_name,'confidence':float(d.confidence),'bbox':list(d.bbox)}for d in detections],
        'hazards':[{'key':list(k),'positives':len(v['positive_samples']),
                    'episode':v.get('episode')}for k,v in self.hazards.items()]})
    return result
V8EventPipeline.hazard_pixels_required=required

old_evidence=V8EventPipeline._evidence

def evidence(self,event,frame_id,frame,*args,**kwargs):
    if event.get('event_type')=='fire_smoke_suspected':
        record({'stage':'hazard_evidence','camera_id':self.camera_id,'frame_id':frame_id,
            'event':event,'pixels':pixels(frame)})
    return old_evidence(self,event,frame_id,frame,*args,**kwargs)
V8EventPipeline._evidence=evidence

old_observe=DlsMotionPolicyGate.observe_models
def observe_models(self,vehicle_hit,smoke_hit,*,fully_evaluated=True):
    with self._lock:
        before={'tracking':self.switch.tracking,'miss':self.switch.miss}
        released=old_observe(self,vehicle_hit,smoke_hit,fully_evaluated=fully_evaluated)
        record({'stage':'motion_model_observation','vehicle_hit':bool(vehicle_hit),
            'smoke_hit':bool(smoke_hit),'fully_evaluated':fully_evaluated,
            'before':before,'after':{'tracking':self.switch.tracking,'miss':self.switch.miss},
            'released':released})
        return released
DlsMotionPolicyGate.observe_models=observe_models
