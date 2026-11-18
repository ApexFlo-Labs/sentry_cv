import collections, openvino as ov
H="/home/admin1/chandana_optimization"
M={"fp16 (current)":f"{H}/cascade/models/vehicle_v11_static.xml",
   "nncf int8 (mine)":f"{H}/int8q/vehicle_v11_int8.xml",
   "qaft int8 (kiranmai)":f"{H}/int8q/qaft/vehicle_qaft_int8_b1.xml"}
core=ov.Core()
def src(o): return o.input_value(0).get_node() if o.get_input_size() else None
def through(n):  # walk back through Convert/Subtract/Multiply/Reshape (weight decompression) to the source
    while n.get_type_name() in ("Convert","Subtract","Multiply","Reshape","Transpose","Unsqueeze") and n.get_input_size():
        n=n.input_value(0).get_node()
    return n
for k,p in M.items():
    m=core.read_model(p)
    convs=[o for o in m.get_ops() if o.get_type_name() in ("Convolution","GroupConvolution","MatMul")]
    q_act=q_w=0
    for c in convs:
        a=c.input_value(0).get_node(); w=c.input_value(1).get_node()
        if a.get_type_name()=="FakeQuantize": q_act+=1
        wt=through(w)
        if w.get_type_name()=="FakeQuantize" or (wt.get_type_name()=="Constant" and str(wt.get_element_type()) in ("<Type: 'int8_t'>","<Type: 'uint8_t'>","i8","u8")) or wt.get_type_name()=="FakeQuantize":
            q_w+=1
    print(f"\n== {k}")
    print(f"   file: {len(convs)} conv/matmul layers, activation INT8-quantised {q_act}, weights INT8 {q_w}")
    cm=core.compile_model(m,"GPU")
    rg=cm.get_runtime_model(); per=collections.Counter(); heavy=collections.Counter()
    for n in rg.get_ops():
        info=n.get_rt_info(); t=str(info["layerType"]) if "layerType" in info else n.get_type_name()
        pr=str(info["runtimePrecision"]) if "runtimePrecision" in info else "?"
        per[pr]+=1
        if "onv" in t or "atMul" in t or "ully" in t: heavy[(t,pr)]+=1
    print("   GPU runtime, all layers by precision:", dict(per))
    print("   GPU runtime, conv/matmul layers:", dict(heavy))
