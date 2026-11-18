import collections, openvino as ov, time, numpy as np
H="/home/admin1/chandana_optimization"
M={"fp16 (current)":f"{H}/cascade/models/vehicle_v11_static.xml",
   "nncf int8 (mine)":f"{H}/int8q/vehicle_v11_int8.xml",
   "qaft int8 (kiranmai)":f"{H}/int8q/qaft/vehicle_qaft_int8_b1.xml"}
core=ov.Core()
def g(info,k):
    try: return info[k].astype(str)
    except Exception:
        try: return str(info[k].value)
        except Exception: return "?"
x=np.random.rand(1,3,640,640).astype(np.float32)
for k,p in M.items():
    cm=core.compile_model(p,"GPU",{"PERF_COUNT":True}); r=cm.create_infer_request()
    for _ in range(20): r.infer({0:x})
    tot=collections.defaultdict(float); cnt=collections.Counter()
    for _ in range(20):
        r.infer({0:x})
        for pi in r.profiling_info:
            if pi.status.name=="EXECUTED":
                key=(pi.node_type, pi.exec_type.split("__")[0][:38])
                tot[key]+=pi.real_time.total_seconds()*1e3/20; cnt[key]+=1
    conv_prec=collections.Counter()
    for n in cm.get_runtime_model().get_ops():
        info=n.get_rt_info()
        if "onvolution" in g(info,"layerType"): conv_prec[g(info,"runtimePrecision")]+=1
    print(f"\n== {k}: GPU convolutions run in: {dict(conv_prec)}   total kernel time per inference {sum(tot.values()):.2f} ms")
    for key,v in sorted(tot.items(),key=lambda kv:-kv[1])[:8]:
        print(f"   {v:6.2f} ms  {cnt[key]//20:3d}x  {key[0]:16s} {key[1]}")
