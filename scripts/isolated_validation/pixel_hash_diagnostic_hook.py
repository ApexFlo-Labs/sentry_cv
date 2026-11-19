"""Hash consumed original-size BGR samples without changing their bytes.

Explicit diagnostic only. Pair PTS/shape/hash across runs; timing is instrumented.
"""


def install():
    import hashlib
    import logging
    from pipeline import surface_stream
    original = surface_stream._map_bgr_any

    def mapped(sample):
        frame = original(sample)
        if frame.shape[1] >= 640:
            logging.getLogger('validation.source_pixels').info('Consumed source pixels', extra={
                'event': 'validation_source_pixels',
                'pts_ns': int(sample.get_buffer().pts), 'shape': list(frame.shape),
                'bgr_sha256': hashlib.sha256(frame.tobytes()).hexdigest()})
        return frame

    surface_stream._map_bgr_any = mapped


if __name__ == 'sitecustomize':
    install()
