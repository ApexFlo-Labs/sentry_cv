"""T2-FIX2: Intel-documented queue directly after gvadetect/gvaclassify (multi-stream) + optional scheduling-policy.
All default OFF -> graph string identical to T2 unless the env vars are set.
  SENTINEL_T2_POSTDET_QUEUE=N   queue (N buffers, never leaky) after primarydet, and after the face chain
  SENTINEL_GVA_SCHEDULING_POLICY=latency|throughput   on primarydet / facedet (shared model instances)"""
import sys
p = sys.argv[1]; s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, (s.count(old), old[:80]); s = s.replace(old, new)
# helpers, defined once near the T2 bgr chain construction
rep("            # T2-B2: same elements as VAMemorySelectiveBgrConverter, inside the graph.\n",
    "            # T2-FIX2: optional queue after GVA inference elements + scheduling policy (default off).\n"
    "            _pdq = int(os.getenv('SENTINEL_T2_POSTDET_QUEUE', '0') or 0)\n"
    "            postdet_queue = (f'queue max-size-buffers={_pdq} max-size-bytes=0 max-size-time=0 ! ' if _pdq > 0 else '')\n"
    "            _pol = os.getenv('SENTINEL_GVA_SCHEDULING_POLICY', '').strip()\n"
    "            sched_policy = (f'scheduling-policy={_pol} ' if _pol in ('latency', 'throughput') else '')\n"
    "            if face_chain:\n"
    "                face_chain = face_chain.replace('pre-process-backend=va-surface-sharing inference-region=roi-list', sched_policy + 'pre-process-backend=va-surface-sharing inference-region=roi-list', 1) + postdet_queue\n"
    "            # T2-B2: same elements as VAMemorySelectiveBgrConverter, inside the graph.\n")
rep("                'batch-size=1 '\n                f'model-instance-id={json.dumps(dlstreamer_model_instance_id(\"vehicle\", name))} ! '\n                f'gvatrack name=objecttracker",
    "                'batch-size=1 ' + sched_policy +\n                f'model-instance-id={json.dumps(dlstreamer_model_instance_id(\"vehicle\", name))} ! ' + postdet_queue +\n                f'gvatrack name=objecttracker")
open(p, 'w').write(s); print('patched', p)
