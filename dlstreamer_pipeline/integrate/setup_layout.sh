#!/bin/bash
# Puts this checkout into the folder layout the pipeline expects and builds the C parts.
#   ROOT (default ~/chandana_optimization) gets:  integrate/  group_ingest/{app,models,secmodels}/
# Run `git lfs pull` first, so the .bin/.onnx model files are real files, not LFS pointers.
# Still needed separately (not code, not in git): ROOT/sentrycv/venv (requirements-engine-venv.txt),
# ROOT/rtsp/mediamtx (v1.21.3, test RTSP server), ROOT/chandana_optimization_videos/*.mp4 (test clips).
set -e
SRC=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$SRC/../.." && pwd); ROOT=${ROOT:-$HOME/chandana_optimization}
for d in "$ROOT/integrate" "$ROOT/group_ingest"; do
  [ -e "$d" ] && { echo "$d already exists: move it away or set ROOT to another folder"; exit 1; }
done
if grep -rlI "^version https://git-lfs" "$SRC" "$REPO/models" --include=*.bin --include=*.onnx | grep -q .; then
  echo "some model files are still LFS pointers: run 'git lfs pull' in the repo first"; exit 1
fi
mkdir -p "$ROOT/integrate" "$ROOT/group_ingest/secmodels"
(cd "$SRC" && tar cf - --exclude=./group_ingest .) | (cd "$ROOT/integrate" && tar xf -)
(cd "$SRC/group_ingest" && tar cf - .) | (cd "$ROOT/group_ingest" && tar xf -)
(cd "$REPO" && tar cf - --exclude=__pycache__ edge_app) | (cd "$ROOT/group_ingest" && tar xf -)  # secondary code (plates + OCR), loaded by group_ingest/app/sec_shared.py
cp "$REPO/models/traffic-v11/openvino/smoke_fire.xml" "$REPO/models/traffic-v11/openvino/smoke_fire.bin" "$ROOT/group_ingest/models/"
for d in traffic-v11 surveillance scene; do cp -r "$REPO/models/$d" "$ROOT/group_ingest/secmodels/"; done
bash "$ROOT/integrate/gatesc/build.sh"
bash "$ROOT/group_ingest/app/build_strip_meta.sh"
echo "layout ready in $ROOT"
for p in "$ROOT/sentrycv/venv" "$ROOT/rtsp/mediamtx" "$ROOT/chandana_optimization_videos"; do
  [ -e "$p" ] || echo "  still needed: $p"
done
