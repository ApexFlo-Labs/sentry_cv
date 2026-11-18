# What are the FP16 detections that INT8 "misses" at conf>=0.3? Borderline scores (threshold jitter) or real misses?
import numpy as np, cv2, openvino as ov
H="/home/admin1/chandana_optimization"
FP=f"{H}/cascade/models/vehicle_v11_static.xml"; Q=f"{H}/int8q/vehicle_v11_int8.xml"
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
core=ov.Core()
def run(p):
    r=core.compile_model(p,"GPU").create_infer_request(); return [r.infer({0:x})[0][0].copy() for x in ev]
a16,a8=run(FP),run(Q)
def iou(a,b):
    x1,y1=max(a[0],b[0]),max(a[1],b[1]); x2,y2=min(a[2],b[2]),min(a[3],b[3]); i=max(0,x2-x1)*max(0,y2-y1)
    return i/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-i+1e-9)
names="bus car minivan motorcycle rickshaw truck bicycle person others".split()
TH=0.3
def analyse(ref,oth,label):
    miss=[]; kinds={"same box, score just below 0.3":0,"same box, other class":0,"no box at all":0}
    bands={}
    for a,b in zip(ref,oth):
        A=[x for x in a if x[4]>=TH]; B=[x for x in b if x[4]>=TH]; Ball=[x for x in b if x[4]>=0.05]; used=set()
        for x in A:
            band="%.1f-%.1f"%(int(x[4]*10)/10,int(x[4]*10)/10+.1); bands.setdefault(band,[0,0]); bands[band][0]+=1
            best,bj=0,-1
            for j,y in enumerate(B):
                if j in used or int(y[5])!=int(x[5]): continue
                v=iou(x,y)
                if v>best: best,bj=v,j
            if best>=0.5: used.add(bj); continue
            bands[band][1]+=1
            low=[y for y in Ball if int(y[5])==int(x[5]) and iou(x,y)>=0.5]
            oc=[y for y in B if int(y[5])!=int(x[5]) and iou(x,y)>=0.5]
            if low: kinds["same box, score just below 0.3"]+=1
            elif oc: kinds["same box, other class"]+=1
            else: kinds["no box at all"]+=1
            miss.append((x[4],(x[2]-x[0])*(x[3]-x[1])))
    print(f"\n{label}: {len(miss)} of {sum(v[0] for v in bands.values())} reference detections not matched")
    for k,v in kinds.items(): print(f"   {k:32s} {v:5d}")
    print("   by reference score:   band   ref dets   unmatched")
    for b in sorted(bands): print(f"                         {b}  {bands[b][0]:6d}   {bands[b][1]:5d} ({100*bands[b][1]/bands[b][0]:.1f}%)")
    m=np.array(miss); print("   unmatched median box area %.0f px (letterbox 640), all-ref median %.0f"%(np.median(m[:,1]),
        np.median([ (x[2]-x[0])*(x[3]-x[1]) for a in ref for x in a if x[4]>=TH])))
analyse(a16,a8,"FP16 detections, looked for in INT8")
analyse(a8,a16,"INT8 detections, looked for in FP16 (the 'extra' ones)")
