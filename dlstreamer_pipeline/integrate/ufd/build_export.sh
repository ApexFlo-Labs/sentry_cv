#!/bin/bash
cd "$(dirname "$0")" || exit 1
L=/opt/intel/dlstreamer/gstreamer/lib; S=/lib/x86_64-linux-gnu
gcc -O2 -Wall -shared -fPIC -o cexport.so.new cexport.c -Wl,--no-undefined $L/libgstva-1.0.so.0 $L/libgstreamer-1.0.so.0 $S/libva.so.2 $S/libglib-2.0.so.0 \
  && mv cexport.so.new cexport.so && echo "built cexport.so" || { rm -f cexport.so.new; echo "BUILD FAILED"; exit 1; }
