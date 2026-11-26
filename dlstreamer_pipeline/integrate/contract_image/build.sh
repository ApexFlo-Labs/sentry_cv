#!/bin/bash
# Builds localhost/sentinel-cv-dls-integrate:<TAG> from this repo's integrate pipeline (the commit checked out, clean)
# + the V8 contract runtime supervisor (runtime/, sentry_cv commit b645c04, unchanged) + contract glue (this folder).
#   ./build.sh                       (TAG=v1, work folder WORK_DIR=~/Documents/dls-contract-image-work)
# Build context, copied base-image libraries and IMAGE_ID go to WORK_DIR, not into the repo.
set -euo pipefail
cd "$(dirname "$0")"
B=$PWD
REPO=$(git -C "$B" rev-parse --show-toplevel)
W=${WORK_DIR:-$HOME/Documents/dls-contract-image-work}; mkdir -p "$W"
C=$W/context; S=$W/sysroot
HEAD_COMMIT=$(git -C "$REPO" rev-parse HEAD)
SRC_COMMIT=${SRC_COMMIT:-$HEAD_COMMIT}
BASE=ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime@sha256:f82000d80084517537a5b014e8228f48815e47962f0ff9fd31002acb448e4718
TAG=${TAG:-v1}
IMAGE=localhost/sentinel-cv-dls-integrate:$TAG

echo "== 1. her code at $SRC_COMMIT, her layout (setup_layout.sh) under /opt/dls-integrate/home/chandana_optimization"
rm -rf "$C"; mkdir -p "$C/dls/home/chandana_optimization"
R=$C/dls/home/chandana_optimization
PATHS="dlstreamer_pipeline/integrate edge_app models/traffic-v11 models/surveillance models/scene"
[ "$HEAD_COMMIT" = "$SRC_COMMIT" ] || { echo "checkout is not at $SRC_COMMIT"; exit 1; }
[ -z "$(git -C "$REPO" status --porcelain -- $PATHS)" ] || { echo "checkout has local changes in $PATHS"; exit 1; }
(cd "$REPO" && git lfs fsck --pointers >/dev/null 2>&1 || true)
if grep -rlI "^version https://git-lfs" $(printf "$REPO/%s " $PATHS) --include=*.bin --include=*.onnx | grep -q .; then
  echo "LFS pointers in the checkout: run git lfs pull in $REPO"; exit 1; fi
(cd "$REPO" && tar cf - --exclude=__pycache__ $PATHS) | tar -x -C "$C"   # clean checkout at SRC_COMMIT (with LFS content)
I=$C/dlstreamer_pipeline/integrate
mkdir -p "$R/integrate" "$R/group_ingest/secmodels"
(cd "$I" && tar cf - --exclude=./group_ingest --exclude=./contract_image --exclude=./run --exclude=./tools_int8 --exclude=./ufd --exclude='./run_*.sh' \
   --exclude=./mock_agent.py --exclude=./loadgen.py --exclude=./cpu_sample.sh --exclude=./setup_layout.sh .) | (cd "$R/integrate" && tar xf -)
(cd "$I/group_ingest" && tar cf - .) | (cd "$R/group_ingest" && tar xf -)
cp -r "$C/edge_app" "$R/group_ingest/edge_app"
cp "$C/models/traffic-v11/openvino/smoke_fire.xml" "$C/models/traffic-v11/openvino/smoke_fire.bin" "$R/group_ingest/models/"
for d in traffic-v11 surveillance scene; do cp -r "$C/models/$d" "$R/group_ingest/secmodels/"; done
ln -s /tmp/sentinel/ov_cache "$R/ov_cache"           # her OpenVINO cache path -> writable /tmp/sentinel
sed -i "s#/home/admin1/chandana_optimization#/opt/dls-integrate/home/chandana_optimization#g" "$R/group_ingest/app/test_nomux.json"
rm -rf "$C/dlstreamer_pipeline" "$C/edge_app" "$C/models"

echo "== 2. contract glue (copies only; every edit must match exactly once)"
cp "$B/contract_glue.py" "$B/motion_gate.py" "$B/face_gate.py" "$R/integrate/"
python3 "$B/apply_contract_patch.py" "$R/integrate"
python3 "$B/t2_parity_patch.py" "$R/integrate"   # T2 parity functions (user decision 2026-11-24)
python3 "$B/face_patch.py" "$R/integrate"        # WHENet pose + clear-face gate (user decision 2026-11-24)
python3 "$B/orig_pixels_patch.py" "$R/integrate" # crops from the original NV12 frame, crop area only (user decision 2026-11-24)
python3 "$B/anpr_patch.py" "$R/integrate"        # ANPR like the face path: plate + OCR C step on NV12 crops (user decision 2026-11-25)
python3 "$B/tracking_patch.py" "$R/integrate"    # tracking test switches: TRACKER_TYPE, REID_ON_NEW (defaults unchanged)
mkdir -p "$C/dls/worker-root/services/worker" "$C/dls/runtime"
cp "$B/worker_root/services/worker/stream_fleet_openvino.py" "$C/dls/worker-root/services/worker/"
cp -r "$B/runtime/traffic_pilot_runtime" "$B/runtime/image_schema" "$B/dls_entrypoint.py" "$C/dls/runtime/"

echo "== 3. C parts (her build scripts' commands) against the base image's own libraries"
if [ ! -f "$S/.done" ]; then
  rm -rf "$S"; mkdir -p "$S"
  podman run --rm --network=none --entrypoint tar "$BASE" -chf - /opt/intel/dlstreamer/gstreamer/lib/libgstanalytics-1.0.so.0 \
     /opt/intel/dlstreamer/gstreamer/lib/libgstvideo-1.0.so.0 /opt/intel/dlstreamer/gstreamer/lib/libgstreamer-1.0.so.0 \
     /opt/intel/dlstreamer/gstreamer/lib/libgstbase-1.0.so.0 /opt/intel/dlstreamer/gstreamer/lib/libgstanalytics-1.0.so.0 \
     /usr/include/openvino /usr/lib/libopenvino_c.so.2026.2.0 /usr/lib/libopenvino.so.2026.2.0 \
     /lib/x86_64-linux-gnu/libgobject-2.0.so.0 /lib/x86_64-linux-gnu/libglib-2.0.so.0 /lib/x86_64-linux-gnu/libva.so.2 \
     2>/dev/null | tar -x -C "$S"
  ln -sf libopenvino_c.so.2026.2.0 "$S/usr/lib/libopenvino_c.so"; touch "$S/.done"
fi
L=$S/opt/intel/dlstreamer/gstreamer/lib; X=$S/lib/x86_64-linux-gnu
LP="-Wl,-rpath-link,$L -Wl,-rpath-link,$S/usr/lib -Wl,-rpath-link,$X"
( cd "$R/integrate/gatesc"
  gcc -O2 -Wall -shared -fPIC -I"$S/usr/include" -o cgates.so cgates.c -Wl,--no-undefined $LP "$S/usr/lib/libopenvino_c.so" \
      "$L/libgstanalytics-1.0.so.0" "$L/libgstvideo-1.0.so.0" "$L/libgstreamer-1.0.so.0" "$X/libgobject-2.0.so.0" "$X/libglib-2.0.so.0" -lm
  rm -f cgates_record.c )   # test only
( cd "$R/group_ingest/app"
  gcc -O2 -Wall -shared -fPIC -o strip_meta.so strip_meta.c -Wl,--no-undefined $LP "$L/libgstreamer-1.0.so.0" "$X/libgobject-2.0.so.0" "$X/libglib-2.0.so.0" )
for so in "$R/integrate/gatesc/cgates.so" "$R/group_ingest/app/strip_meta.so"; do
  need=$(objdump -T "$so" | grep -o "GLIBC_[0-9.]*" | sort -uV | tail -1); echo "built $(basename $so), newest glibc symbol $need (image has 2.39)"
done

echo "== 4. image $IMAGE"
"$B/gpu_drivers/fetch_debs.sh" "$C/gpu_drivers"   # Intel GPU drivers as on argus-2 (no network inside the build)
cp "$B/Dockerfile" "$C/"
( cd "$C" && find dls -type f ! -name "*.so" -print0 | sort -z | xargs -0 sha256sum ) > "$C/dls/SOURCE_SHA256SUMS"
echo "$SRC_COMMIT" > "$C/dls/SOURCE_COMMIT"
podman build --network=none --build-arg BASE="$BASE" --build-arg SRC_COMMIT="$SRC_COMMIT" -t "$IMAGE" "$C"
podman image inspect "$IMAGE" --format '{{.Id}}' | tee "$W/IMAGE_ID"
echo "BUILD DONE $IMAGE"
