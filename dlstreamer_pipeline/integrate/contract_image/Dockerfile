# Chandana's DL Streamer integrate pipeline (her engine + V8 event code b645c04) as a V8-contract CV runtime image.
# Base: the published V8 runtime v8-r2, used ONLY for its OS / DL Streamer 2026.2 / OpenVINO 2026.2 / GPU+NPU user
# space and uid 10001; our V8 application and models are removed below and never run in this image.
ARG BASE
FROM ${BASE}
ARG SRC_COMMIT
USER 0:0
RUN rm -rf /opt/traffic-pilot /models /opt/sentinel-gst-plugins
# Ubuntu's python3-gst-1.0 (GStreamer 1.24 bindings) shadows DL Streamer's 1.28 bindings for the same files; her engine
# was measured with DL Streamer's (Gst.init(None) fails with Ubuntu's). Remove only the files DL Streamer also ships.
RUN cd /usr/lib/python3/dist-packages/gi/overrides && rm -f Gst.py GstAudio.py GstPbutils.py GstVideo.py _gi_gst.cpython-312-x86_64-linux-gnu.so __pycache__/Gst*.pyc
COPY --chown=0:0 --chmod=755 dls /opt/dls-integrate
# fixed-shape SigLIP for the NPU: her engine writes it on first start, but the root filesystem is read-only at run time
RUN python3 -c "import openvino as ov; m = ov.Core().read_model('/opt/dls-integrate/home/chandana_optimization/group_ingest/secmodels/scene/openvino/siglip2-base-patch16-224.xml'); m.reshape([1, 3, 224, 224]); ov.save_model(m, '/opt/dls-integrate/home/chandana_optimization/integrate/models/siglip2-base-patch16-224-static.xml')" \
 && python3 -m compileall -q /opt/dls-integrate/home /opt/dls-integrate/runtime /opt/dls-integrate/worker-root

# V8 contract runtime (supervisor): desired state, readiness, health, metrics, delivery socket
ENV TRAFFIC_PILOT_ROOT=/opt/dls-integrate/worker-root \
    DLS_INTEGRATE_ROOT=/opt/dls-integrate/home/chandana_optimization \
    HOME=/opt/dls-integrate/home \
    PYTHONPATH=/opt/dls-integrate/runtime:/opt/intel/dlstreamer/gstreamer/lib/python3/dist-packages:/opt/intel/dlstreamer/python \
    DESIRED_STATE_PATH=/configs/desired_state.json \
    APEXFABRIC_SECRETS_ROOT=/run/secrets/sentinel/cameras \
    APEXFABRIC_STATE_ROOT=/tmp/sentinel/state \
    APEXFABRIC_GENERATED_DIR=/tmp/sentinel/generated \
    APEXFABRIC_PLAN_DIR=/plans \
    SNAPSHOT_ROOT=/tmp/sentinel/state/snapshots \
    ANALYTICS_EVENT_LOG_PATH=/tmp/sentinel/state/events/analytics.jsonl \
    SENTINEL_CV_HANDOFF_STATUS_PATH=/tmp/sentinel/generated/v8_handoff_status.json \
    SENTINEL_DELIVERY_AGENT_SOCKET=/run/sentinel-delivery/agent.sock \
    SENTINEL_TELEMETRY_INTERVAL_SECONDS=2 \
    WORKER_CAMERA_INITIALIZATION_SECONDS=600 \
    APEX_API_PORT=8080
# her final configuration (run_best20.sh + run_full9.sh + run_integrate.sh defaults); test/measurement switches are not set
ENV HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 \
    PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1 \
    VEH_MODEL=/opt/dls-integrate/home/chandana_optimization/integrate/models/vehicle_qaft_int8_b1.xml \
    CPU_OPT=threads CPU_OPT_THREADS=2 SCENE_DEVICE=NPU BODY_DEVICE=NPU GAIT_DEVICE=NPU HEAD_POSE_DEVICE=NPU \
    WARMUP_S=120 T_GAIT_SCALE=1

LABEL org.opencontainers.image.title="sentinel-cv-dls-integrate" \
      io.apexfabric.image.role="cv-runtime" \
      io.apexfabric.contract.version="8.0" \
      io.apexfabric.contract.status="development" \
      io.apexfabric.multi-box.delivery.version="7.0" \
      io.apexfabric.delivery-agent.protocol.version="1.0" \
      io.apexfabric.hardware.profile="intel-285h" \
      io.apexfabric.max-configured-cameras="50" \
      io.apexfabric.models.delivery="baked-in" \
      io.apexfabric.engine="chandana-integrate9" \
      io.apexfabric.engine.source-commit="${SRC_COMMIT}" \
      io.apexfabric.v8-event-code.commit="b645c04" \
      io.apexfabric.runtime-supervisor.commit="b645c04"
WORKDIR /opt/dls-integrate
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/opt/dls-integrate/runtime/dls_entrypoint.py"]
