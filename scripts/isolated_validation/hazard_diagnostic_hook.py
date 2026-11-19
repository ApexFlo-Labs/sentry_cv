"""Read-only native hazard scores and temporal state for injected test runs.

Never changes detections, confidence, geometry, thresholds, scheduling or output.
"""


def install():
    import logging
    import threading
    import time
    from pipeline import dlstreamer_bridge as bridge
    from pipeline.v8_events import V8EventPipeline
    original_read = bridge.hazards_from_sample
    original_policy = V8EventPipeline._hazards
    sampled = {}

    def read(sample):
        detections = original_read(sample)
        key = ('detector', threading.get_ident())
        now = time.monotonic()
        if now-sampled.get(key, -1000) >= 1:
            sampled[key] = now
            logging.getLogger('validation.hazard').info('Unmodified native hazard observations', extra={
                'event': 'validation_hazard_detector',
                'pts_ns': int(sample.get_buffer().pts),
                'detections': [{'label': v.class_name, 'confidence': float(v.confidence),
                                'bbox': list(v.bbox)} for v in detections]})
        return detections

    def policy(self, packet, now, frame_id):
        result = original_policy(self, packet, now, frame_id)
        key = ('policy', self.camera_id)
        tick = time.monotonic()
        if (getattr(packet, 'analytics_state', {}).get('fire_smoke_evaluated', True)
                and tick-sampled.get(key, -1000) >= 1):
            sampled[key] = tick
            logging.getLogger('validation.hazard').info('Unmodified hazard confirmation state', extra={
                'event': 'validation_hazard_policy', 'camera_id': self.camera_id,
                'frame_shape': list(packet.frame.shape), 'observed_at': now,
                'effective_gate': dict(self.policy.section('fire_smoke')),
                'states': [{'key': list(k), 'positive_count': len(v['positive_samples']),
                            'scores': [float(s[1]) for s in v['positive_samples']],
                            'negative_count': v['negative_count'], 'episode': v['episode']}
                           for k, v in self.hazards.items()]})
        return result

    bridge.hazards_from_sample = read
    V8EventPipeline._hazards = policy


if __name__ == 'sitecustomize':
    install()
