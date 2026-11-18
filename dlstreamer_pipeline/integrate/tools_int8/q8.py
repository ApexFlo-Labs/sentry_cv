# Quantise the vehicle detector to INT8 (NNCF post-training), then compare vs FP16 on held-out frames.
import sys, time, numpy as np, cv2, openvino as ov, nncf
H="/home/admin1/chandana_optimization"
SRC=f"{H}/cascade/models/vehicle_v11_static.xml"
OUT=f"{H}/int8q/vehicle_v11_int8.xml"
V=f"{H}/chandana_optimization_videos"
CLIPS=["plate_clip_35-45min.mp4","PCR2_18h.mp4","PCR2_10h.mp4","NVR_ch14.mp4"]
def letter(img):
    h,w=img.shape[:2]; s=min(640/h,640/w); nh,nw=int(round(h*s)),int(round(w*s))
    r=cv2.resize(img,(nw,nh)); c=np.full((640,640,3),114,np.uint8)
    t,l=(640-nh)//2,(640-nw)//2; c[t:t+nh,l:l+nw]=r
    return (c[:,:,::-1].transpose(2,0,1)[None].astype(np.float32)/255.0)
def frames(n_per, offset):
    out=[]
    for c in CLIPS:
        cap=cv2.VideoCapture(f"{V}/{c}"); N=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        for i in range(n_per):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i+offset)*N/(n_per+1))%N); ok,f=cap.read()
            if ok: out.append(letter(f))
    return out
calib=frames(75,0.5)   # 300 calibration frames
evalf=frames(100,0.0)  # 400 different frames for comparison
print("calib",len(calib),"eval",len(evalf),flush=True)
core=ov.Core(); m=core.read_model(SRC)
if "--skip" not in sys.argv:
    t=time.time()
    q=nncf.quantize(m, nncf.Dataset(calib), preset=nncf.QuantizationPreset.MIXED, subset_size=300)
    ov.save_model(q,OUT); print("quantised in %.0fs"%(time.time()-t),flush=True)
def run(path):
    cm=core.compile_model(path,"GPU",{"PERFORMANCE_HINT":"THROUGHPUT"}); r=cm.create_infer_request()
    dets=[r.infer({0:x})[0][0].copy() for x in evalf]
    q=ov.AsyncInferQueue(cm,6); t=time.time(); K=600
    for i in range(K): q.start_async({0:evalf[i%len(evalf)]})
    q.wait_all(); return dets, K/(time.time()-t)
def iou(a,b):
    x1,y1=max(a[0],b[0]),max(a[1],b[1]); x2,y2=min(a[2],b[2]),min(a[3],b[3])
    i=max(0,x2-x1)*max(0,y2-y1); return i/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-i+1e-9)
d16,f16=run(SRC); d8,f8=run(OUT)
print("GPU throughput alone: FP16 %.0f inf/s  INT8 %.0f inf/s"%(f16,f8))
TH=0.3; names="bus car minivan motorcycle rickshaw truck bicycle person others".split()
st={}; ious=[]; sd=[]
for a,b in zip(d16,d8):
    A=[x for x in a if x[4]>=TH]; B=[x for x in b if x[4]>=TH]; used=set()
    for x in A:
        k=names[int(x[5])]; s=st.setdefault(k,[0,0,0]); s[0]+=1
        best,bj=0,-1
        for j,y in enumerate(B):
            if j in used or int(y[5])!=int(x[5]): continue
            v=iou(x,y)
            if v>best: best,bj=v,j
        if best>=0.5: used.add(bj); s[1]+=1; ious.append(best); sd.append(B[bj][4]-x[4])
    for j,y in enumerate(B):
        if j not in used: st.setdefault(names[int(y[5])],[0,0,0])[2]+=1
print("per class (FP16 dets at conf>=0.3 | kept by INT8 | extra INT8 dets)")
T=[0,0,0]
for k,s in sorted(st.items(),key=lambda kv:-kv[1][0]):
    print("  %-10s fp16 %5d  kept %5d (%.1f%%)  extra %4d"%(k,s[0],s[1],100*s[1]/max(1,s[0]),s[2])); T=[T[i]+s[i] for i in range(3)]
print("TOTAL fp16 %d kept %d (%.1f%%) extra %d | mean IoU %.3f | mean score change %+.3f"%(T[0],T[1],100*T[1]/max(1,T[0]),T[2],np.mean(ious),np.mean(sd)))
