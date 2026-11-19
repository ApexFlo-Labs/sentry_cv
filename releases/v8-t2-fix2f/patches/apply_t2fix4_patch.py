"""T2-FIX4 (user rule: no code inside models, face steps separate): face uses the PRODUCTION Python face path
(OpenVINOFaceExtractor: plain scrfd_500m.onnx -> production decode/NMS -> SixDRepNet best_a0_fp16 pose gate ->
cv2.estimateAffinePartial2D alignment -> plain adaface_ir101_int8), instead of the graph chain with the modified models
scrfd_top1x_u8 (decode+top1+1.6x square inside) and face_align_u8 (2nd SCRFD + align + AdaFace merged).
Same FaceSamplePipeline scheduling (FACE_PROCESS_INTERVAL=3, adaptive due scheduler, unchanged gates).
SENTINEL_FACE_PLAIN=0 restores the old graph chain."""
import sys
p = sys.argv[1]; s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:70]; s = s.replace(old, new)
rep("""        if dls_authoritative:
            from pipeline.dlstreamer_bridge import DlsFaceExtractor
""", """        if dls_authoritative and os.getenv("SENTINEL_FACE_PLAIN", "1") == "0":
            from pipeline.dlstreamer_bridge import DlsFaceExtractor
""")
rep("""    if dls_authoritative and enable_face:
        from pipeline.dlstreamer_bridge import DlsFaceMetadataBridge
""", """    if dls_authoritative and enable_face and os.getenv("SENTINEL_FACE_PLAIN", "1") == "0":
        from pipeline.dlstreamer_bridge import DlsFaceMetadataBridge
""")
open(p, 'w').write(s); print('ok')
