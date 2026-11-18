# Compare detectors on the same 400 held-out frames (same frames as the NNCF INT8 test), vs our FP16 as reference.
import numpy as np, cv2, openvino as ov, time
H="/home/admin1/chandana_optimization"
MODELS={"fp16 (current)":f"{H}/cascade/models/vehicle_v11_static.xml",
        "nncf int8 (mine)":f"{H}/int8q/vehicle_v11_int8.xml",
        "qaft int8 (kiranmai)":f"{H}/int8q/qaft/vehicle_qaft_int8_b1.xml"}
V=f"{H}/chandana_optimization_videos"; CLIPS=["plate_clip_35-45min.mp4","PCR2_18h.mp4","PCR2_10h.mp4","NVR_ch14.mp4"]
def letter(img):
    h,w=img.shape[:2]; s=min(640/h,640/w); nh,nw=int(round(h*s)),int(round(w*s))
    c=np.full((640,640,3),114,np.uint8); t,l=(640-nh)//2,(640-nw)//2; c[t:t+nh,l:l+nw]=cv2.resize(img,(nw,nh))
    return c[:,:,::-1].transpose(2,0,1)[None].astype(np.float32)/255.0
ev=[]
for c in CLIPS:
    cap=cv2.VideoCapture(f"{V}/{c}"); N=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    for i in range(100):
        cap.set(cv2.CAP_PROP_POS_FRAMES,int(i*N/101)%N); ok,f=cap.read()
        if ok: ev.append(letter(f))
core=ov.Core(); names="bus car minivan motorcycle rickshaw truck bicycle person others".split()
def run(p):
    m=core.read_model(p); print("  ",p.split("/")[-1],"in",m.inputs[0].get_partial_shape(),"out",m.outputs[0].get_partial_shape(),
        "FakeQuantize",sum(o.get_type_name()=="FakeQuantize" for o in m.get_ops()))
    cm=core.compile_model(m,"GPU",{"PERFORMANCE_HINT":"THROUGHPUT"}); r=cm.create_infer_request()
    d=[r.infer({0:x})[0][0].copy() for x in ev]
    q=ov.AsyncInferQueue(cm,6); t=time.time()
    for i in range(600): q.start_async({0:ev[i%len(ev)]})
    q.wait_all(); return d,600/(time.time()-t)
def iou(a,b):
    x1,y1=max(a[0],b[0]),max(a[1],b[1]); x2,y2=min(a[2],b[2]),min(a[3],b[3]); i=max(0,x2-x1)*max(0,y2-y1)
    return i/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-i+1e-9)
res={k:run(p) for k,p in MODELS.items()}
TH=0.3; ref=res["fp16 (current)"][0]
for k,(d,fps) in res.items():
    tot=sum((x[:,4]>=TH).sum() for x in d)
    print(f"\n{k}: GPU alone {fps:.0f} inf/s, detections at conf>=0.3: {tot}")
    if k.startswith("fp16"): continue
    st={}; ious=[]
    for a,b in zip(ref,d):
        A=[x for x in a if x[4]>=TH]; B=[x for x in b if x[4]>=TH]; used=set()
        for x in A:
            s=st.setdefault(names[int(x[5])],[0,0,0]); s[0]+=1; best,bj=0,-1
            for j,y in enumerate(B):
                if j in used or int(y[5])!=int(x[5]): continue
                v=iou(x,y)
                if v>best: best,bj=v,j
            if best>=0.5: used.add(bj); s[1]+=1; ious.append(best)
        for j,y in enumerate(B):
            if j not in used: st.setdefault(names[int(y[5])],[0,0,0])[2]+=1
    T=[0,0,0]
    for c,s in sorted(st.items(),key=lambda kv:-kv[1][0]):
        print("  %-10s fp16 %5d  same %5d (%5.1f%%)  only-this-model %4d"%(c,s[0],s[1],100*s[1]/max(1,s[0]),s[2])); T=[T[i]+s[i] for i in range(3)]
    print("  TOTAL      fp16 %5d  same %5d (%5.1f%%)  only-this-model %4d | mean IoU %.3f"%(T[0],T[1],100*T[1]/max(1,T[0]),T[2],np.mean(ious)))
