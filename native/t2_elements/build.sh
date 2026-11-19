#!/bin/sh
# Build recipe for the Sentinel elements (recovered from the 2026-11-16 build commands; image GStreamer headers/libs,
# OpenCV 4.14.0 static build like the image's cv2, OpenVINO 2026.2 SDK).  usage: build.sh <element-dir>
set -e
B=/home/admin1/Documents/sentry_target_build
I=$B/tools/opencv/install; SDK=$B/tools/image_sdk
HK=/home/admin1/Documents/sentry_face_pipeline_investigation_2026-11-12/hook1_custom_postproc_test
cd $B/elements/$1
g++ -std=c++17 -O2 -Wall -fPIC -shared -o libgst$1.so gst$1.cpp -I ../common -I $SDK/include_openvino -I $I/include/opencv4 \
  -I $HK/image_headers/gstreamer-1.0 $(pkg-config --cflags glib-2.0 gobject-2.0) -L $HK/image_libs -L $SDK/lib -L $I/lib \
  -L $I/lib/opencv4/3rdparty -lgstvideo-1.0 -lgstbase-1.0 -lgstreamer-1.0 -lopencv_imgproc -lopencv_core -lipphal -lippiw \
  -lippicv -lpthread -ldl -lz $(pkg-config --libs glib-2.0 gobject-2.0) -Wl,--exclude-libs,ALL -Wl,--allow-shlib-undefined
ls -la libgst$1.so
