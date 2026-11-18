#!/bin/bash
# Builds strip_meta.so (removes the mux batch meta before gvametaconvert; used by integrate9.py) against DL Streamer's GStreamer.
cd "$(dirname "$0")" || exit 1
L=/opt/intel/dlstreamer/gstreamer/lib; S=/lib/x86_64-linux-gnu
gcc -O2 -Wall -shared -fPIC -o strip_meta.so.new strip_meta.c -Wl,--no-undefined $L/libgstreamer-1.0.so.0 $S/libgobject-2.0.so.0 $S/libglib-2.0.so.0 \
  && mv strip_meta.so.new strip_meta.so && echo "built strip_meta.so" || { rm -f strip_meta.so.new; echo "BUILD FAILED"; exit 1; }
