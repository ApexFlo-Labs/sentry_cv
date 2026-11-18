#!/bin/bash
# Builds cgates.so (live gates + head-pose gate) and cgates_record.so (test only) against the GStreamer libraries
# DL Streamer uses (no headers needed); fails on any unknown symbol. Builds to a temporary file and replaces the
# library only on success, so a failed build never removes a working cgates.so.
cd "$(dirname "$0")" || exit 1
L=/opt/intel/dlstreamer/gstreamer/lib; S=/lib/x86_64-linux-gnu
LIBS="/usr/lib/libopenvino_c.so $L/libgstanalytics-1.0.so.0 $L/libgstvideo-1.0.so.0 $L/libgstreamer-1.0.so.0 $S/libgobject-2.0.so.0 $S/libglib-2.0.so.0 -lm"
for t in cgates cgates_record; do
  gcc -O2 -Wall -shared -fPIC -o $t.so.new $t.c -Wl,--no-undefined $LIBS && mv $t.so.new $t.so && echo "built $t.so" || { rm -f $t.so.new; echo "BUILD FAILED $t"; exit 1; }
done
