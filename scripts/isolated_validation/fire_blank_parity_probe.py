#!/usr/bin/env python3
"""Exclusive baked-image fire diagnostic, not a production device fallback.

Compares identical uniform BGR/RGB pixels via direct OpenVINO CPU/GPU and
native GVA GPU preprocessing/conversion. No threshold/model/policy changes.
Run under the existing exclusive resource guard, after capacity measurements.
"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import time
import sys

import numpy as np


def raw_scores(output):
    values=np.asarray(output)
    if values.shape!=(1,6,2100) or not np.isfinite(values).all():
        raise ValueError('Expected finite smoke_fire [1,6,2100] output')
    return {'fire':float(values[0,4].max()),'smoke':float(values[0,5].max())}


def direct(model_path,device,levels,*,fp32=False,disable_lp=False):
    import openvino as ov
    core=ov.Core()
    config={'INFERENCE_PRECISION_HINT':'f32'} if fp32 else {}
    if disable_lp:
        if not device.startswith('GPU'):
            raise ValueError('LP_TRANSFORMS_MODE diagnostic is GPU-only')
        config['LP_TRANSFORMS_MODE']=False
    if device=='CPU':config['INFERENCE_NUM_THREADS']=1
    elif device.startswith('GPU'):config['COMPILATION_NUM_THREADS']=1
    elif fp32:raise ValueError('No unvalidated precision override for this target')
    model=core.compile_model(core.read_model(model_path),device,config)
    results=[]
    for level in levels:
        image=np.full((1,3,320,320),level/255.,dtype=np.float32)
        results.append({'level':level,'max_raw_class_scores':raw_scores(model([image])[0])})
    try:reported_precision=str(model.get_property('INFERENCE_PRECISION_HINT'))
    except RuntimeError:reported_precision='property unavailable on this device'
    try:reported_lp=model.get_property('LP_TRANSFORMS_MODE')
    except RuntimeError:reported_lp='property unavailable on this device'
    return {'device':device,'explicit_config':config,'actual_devices':model.get_property('EXECUTION_DEVICES'),
            'reported_lp_transformations':reported_lp,
            'reported_precision_hint':reported_precision,
            'results':results}


def native(model_path,proc_path,levels,*,fp32=False,par='1/1',nireq=1,source_fps=1,batch=1,shared=False):
    sys.path.insert(0,'/opt/traffic-pilot/services/worker')
    import gi
    gi.require_version('Gst','1.0')
    from gi.repository import Gst
    from gstgva import VideoFrame
    from pipeline.gva_inference_options import gpu_compilation_ie_config
    Gst.init([])
    compile_options=gpu_compilation_ie_config('GPU')
    if fp32:
        configured=json.loads(compile_options.split('=',1)[1].strip())
        compile_options='ie-config='+json.dumps(configured+',INFERENCE_PRECISION_HINT=f32')+' '
    share_option=('model-instance-id='+json.dumps(f'validation-fire-b{batch}-fp32{fp32}')+' ')if shared else ''
    graph=('appsrc name=source is-live=true format=time block=false '
        f'caps=video/x-raw,format=BGR,width=320,height=320,pixel-aspect-ratio={par},framerate={source_fps}/1 ! '
        f'gvadetect model={json.dumps(str(model_path))} model-proc={json.dumps(str(proc_path))} '
        'device=GPU '+compile_options+share_option+
        f'pre-process-backend=opencv threshold=0.35 nireq={nireq} batch-size={batch} ! '
        'appsink name=out sync=false async=false max-buffers=1 drop=false enable-last-sample=false')
    pipeline=Gst.parse_launch(graph);rows=[]
    try:
        assert pipeline.set_state(Gst.State.PLAYING)!=Gst.StateChangeReturn.FAILURE
        source=pipeline.get_by_name('source');sink=pipeline.get_by_name('out')
        for index,level in enumerate(levels):
            image=np.full((320,320,3),level,dtype=np.uint8)
            data=image.tobytes();buffer=Gst.Buffer.new_allocate(None,len(data),None)
            buffer.fill(0,data);buffer.pts=index*Gst.SECOND;buffer.duration=Gst.SECOND
            assert source.emit('push-buffer',buffer)==Gst.FlowReturn.OK
            sample=sink.emit('try-pull-sample',30*Gst.SECOND)
            if sample is None:
                message=pipeline.get_bus().timed_pop_filtered(0,Gst.MessageType.ERROR)
                detail=str(message.parse_error()) if message else 'no native output within30s'
                raise RuntimeError(detail)
            assert sample.get_buffer().pts==buffer.pts,'Native diagnostic frame order changed'
            frame=VideoFrame(sample.get_buffer(),caps=sample.get_caps())
            rois=[{'label':str(roi.label()),'confidence':float(roi.confidence()),
                   'bbox':[int(getattr(roi.rect(),key)) for key in ['x','y','w','h']]}for roi in frame.regions()]
            rows.append({'level':level,'input_pixels_sha256':hashlib.sha256(data).hexdigest(),
                         'native_rois':rois})
        return {'device':'GPU/DLStreamer','explicit_fp32_hint':fp32,'pixel_aspect_ratio':par,
                'nireq':nireq,'batch_property':batch,'shared_model_id':shared,
                'source_fps':source_fps,'pipeline':graph,'results':rows}
    finally:
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(5*Gst.SECOND)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',type=Path,default=Path('/models/traffic/openvino/smoke_fire.xml'))
    parser.add_argument('--model-proc',type=Path,default=Path('/opt/traffic-pilot/config/fire_rgb_model_proc.json'))
    parser.add_argument('--native-aspect-matrix',action='store_true',
                        help='Only compare black native inputs across actual aspect/request settings')
    parser.add_argument('--native-auto-batch-matrix',action='store_true',
                        help='Compare the integrated auto-batch and sharing settings on black pixels')
    parser.add_argument('--native-first',action='store_true',
                        help='Initialize native GVA before importing Python OpenVINO')
    parser.add_argument('--npu-only',action='store_true',
                        help='Explicit NPU diagnostic only; not a production fallback or placement change')
    parser.add_argument('--gpu-lp-matrix',action='store_true',
                        help='Direct GPU-only compiler hypothesis, not a native graph fix or capacity test')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    levels=([0,0,0] if args.native_auto_batch_matrix or args.native_first else [0] if args.native_aspect_matrix
            else [0,16,32,64,96,114,128,160,192,224,255])
    result={'scope':__doc__,'started_unix':time.time(),'levels':levels,'runs':[],
        'files_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [args.model,args.model.with_suffix('.bin'),args.model_proc]}}
    def save(): (args.output/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        if args.native_first:
            result['runs'].append(native(args.model,args.model_proc,levels,
                par='16/9',nireq=2,source_fps=30,batch=0,shared=True));save()
            paths=sorted({line.split()[-1]for line in Path('/proc/self/maps').read_text().splitlines()
                if any(name in line for name in ['libopenvino','opencv','dlstreamer'])
                and line.split()[-1].startswith('/')})
            result['native_first_libraries_before_python_openvino']=paths
            import openvino as ov
            result['openvino_version_after_native']=ov.__version__
        else:
            import openvino as ov
            result['openvino_version']=ov.__version__
        if args.gpu_lp_matrix:
            result['property_source']='https://github.com/openvinotoolkit/openvino/blob/2026.2.0/src/inference/dev_api/openvino/runtime/internal_properties.hpp'
            result['property_scope']='Internal diagnostic option, not a production API/default change'
            for fp32,disable_lp in [(False,False),(False,True),(True,False),(True,True)]:
                result['runs'].append(direct(args.model,'GPU',levels,fp32=fp32,disable_lp=disable_lp))
                save();gc.collect()
        elif args.npu_only:
            result['runs'].append(direct(args.model,'NPU',levels));save()
        elif args.native_first:
            pass
        elif args.native_auto_batch_matrix:
            for batch,shared,fp32 in [(1,False,False),(0,False,False),
                    (1,True,False),(0,True,False),(0,True,True)]:
                result['runs'].append(native(args.model,args.model_proc,levels,fp32=fp32,
                    par='16/9',nireq=2,source_fps=30,batch=batch,shared=shared));save();gc.collect()
        elif args.native_aspect_matrix:
            for fp32,par,nireq in [(False,'1/1',1),(False,'16/9',1),
                    (False,'1/1',2),(False,'16/9',2),(True,'16/9',2)]:
                result['runs'].append(native(args.model,args.model_proc,levels,
                    fp32=fp32,par=par,nireq=nireq,source_fps=30));save();gc.collect()
        else:
            for device,fp32 in [('CPU',True),('GPU',False),('GPU',True)]:
                result['runs'].append(direct(args.model,device,levels,fp32=fp32));save();gc.collect()
            for fp32 in [False,True]:
                result['runs'].append(native(args.model,args.model_proc,levels,fp32=fp32));save();gc.collect()
        result['execution_complete']=True
    except Exception as exc:
        result.update(execution_complete=False,error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        result['ended_unix']=time.time();save()
    print(json.dumps({'execution_complete':True,'report':str(args.output/'report.json'),
        'note':'Diagnostic execution only, not model/gate qualification or production fallback'}))


if __name__=='__main__':main()
