"""Read-only ROI-label sampling for an explicitly injected diagnostic run.

Mounted as sitecustomize.py only by --diagnostic-hook. It never removes ROIs,
changes tracking, filters detections or claims an uninstrumented capacity pass.
"""


def install():
    from collections import Counter
    import logging
    import threading
    import time
    from pipeline import dlstreamer_bridge as bridge
    original = bridge.detections_from_sample
    last = {}

    def sampled(sample, class_names):
        identity = threading.get_ident()
        now = time.monotonic()
        if now - last.get(identity, -1000) >= 5:
            last[identity] = now
            try:
                from gstgva import VideoFrame
                frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
                labels = Counter(str(r.label() or '') for r in frame.regions())
                logging.getLogger('validation.roi_metadata').info(
                    'Diagnostic ROI label distribution; output is unchanged',
                    extra={'event':'validation_roi_labels', 'roi_label_counts':dict(labels),
                           'camera_worker_thread':threading.current_thread().name})
            except Exception:
                logging.getLogger('validation.roi_metadata').exception('ROI diagnostic sampling failed')
        return original(sample, class_names)

    bridge.detections_from_sample = sampled


if __name__ == 'sitecustomize':
    install()
