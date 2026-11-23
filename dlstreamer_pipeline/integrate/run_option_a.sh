#!/bin/bash
# Option A (2026-11-20): run_best20.sh + license plates inside the graph on native frames, plates and faces every 5th
# frame, per-vehicle plate state and "no change -> no plate inference" gate (see README, "Plates inside the graph").
# Measured on argus-2 (Core Ultra 9 285H), 20 cameras: 160.9 fps total (5.0-6.8 per camera), plate boxes 99 % on the
# plate, 0 crashes. Usage: ./run_option_a.sh   (CAMS=4 ./run_option_a.sh for a quick check)
cd "$(dirname "$0")" || exit 1
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 QUEUE_MON=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1
export VEH_MODEL=${VEH_MODEL:-$PWD/models/vehicle_qaft_int8_b1.xml}
# plates: in-graph plate detector + OCR (NPU, crops prepared by OpenCV from the native NV12 frame), vehicles >= 80 px,
# every 5th frame picked by the C step, confirmed plates skipped 60 s, unchanged vehicles skipped (moved/grew/3 s)
export PLATE_INLINE=1 PLATE_SELECT=1 PLATE_VEH_MIN_W=${PLATE_VEH_MIN_W:-80} PLATE_PREPROC=opencv PLATE_NIREQ=${PLATE_NIREQ:-4} \
       PLATE_INTERVAL=1 PLATE_C_EVERY=${PLATE_C_EVERY:-5} PLATE_STATE=1 PLATE_CHANGE=1
# faces (SCRFD + head pose + AdaFace) every 5th frame; fire/smoke stays on the GPU (2 frames/s)
export FACE_INTERVAL=${FACE_INTERVAL:-5} FIRE_DEVICE=${FIRE_DEVICE:-GPU}
CAMS=${CAMS:-20} DUR=${DUR:-420} ./run_full9.sh
