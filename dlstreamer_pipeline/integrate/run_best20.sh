#!/bin/bash
# Final configuration (2026-11-18): 20 cameras x 10 fps, Kiranmai's V8 rules (unchanged), her QAFT INT8 vehicle detector.
# Measured: 209.4 fps total with the 640x360 plate branch (engine-only ceiling 240), 0 contract errors; since plates/OCR
# read native frames (SEC_NATIVE, default on) 195.3 fps; SEC_NATIVE=0 restores the 640x360 branch. Plates inside the
# graph with plates + faces every 5th frame: run_option_a.sh. Usage: ./run_best20.sh   (CAMS=4 ./run_best20.sh for a quick check)
cd "$(dirname "$0")" || exit 1
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 QUEUE_MON=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1
export VEH_MODEL=${VEH_MODEL:-$PWD/models/vehicle_qaft_int8_b1.xml}   # FP16 fallback: VEH_MODEL= (empty) uses group_ingest/models/vehicle_v11_static.xml
CAMS=${CAMS:-20} DUR=${DUR:-420} ./run_full9.sh
