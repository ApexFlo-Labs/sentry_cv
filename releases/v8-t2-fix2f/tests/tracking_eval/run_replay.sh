#!/bin/sh
# run the T2 detector+tracker+body replay for all 7 MOT17 sequences inside the local T2 image
cd /home/admin1/Documents/sentry_target_build
for S in 02 04 09 10 11 13; do
  podman run --rm --network none --user 0 --group-add keep-groups --device /dev/dri --device /dev/accel \
    -v $PWD/tests/tracking_eval:/w:Z -v /home/admin1/Documents/IMAGE_TESTS/datasets/mot17/MOT17/train:/mot:ro,Z \
    --entrypoint python3 localhost/sentinel-cv:v8-t2-full-local /w/replay_mot.py /mot/MOT17-$S-SDP /w/out/MOT17-$S-SDP.json 8 2>&1 | grep -E "DONE|Error|Traceback|error" | tail -5
done
