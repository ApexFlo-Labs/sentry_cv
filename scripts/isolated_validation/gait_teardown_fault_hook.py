"""Diagnostic one-shot camera exception after scene delivery admission.

Run with gait configured, including a quiet source. This tests native teardown
and owner restart, not positive gait execution, accuracy or capacity. The peer
must already have time to start; the normal frame/quality gates are untouched.
"""
import json
import logging
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.dls_secondary import DlsGaitExtractor
from pipeline.v8_events import V8EventPipeline

MARKER = Path('/tmp/sentry-gait-teardown-once')
logger = logging.getLogger('test_gait_teardown')
original_scene = V8EventPipeline._scene
original_close = DlsGaitExtractor.close


class InjectedGaitTeardown(ValueError):
    pass


def scene(self, *args, **kwargs):
    result = original_scene(self, *args, **kwargs)
    if self.camera_id != 'capacity-01' or MARKER.exists():
        return result
    now = time.monotonic()
    started = getattr(self, '_test_teardown_scene_started', None)
    if started is None:
        self._test_teardown_scene_started = now
        return result
    if now-started < 20:
        return result
    if self.gait is None:
        raise AssertionError('fault fixture requires a gait-configured camera')
    try:
        fd = os.open(MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return result
    with os.fdopen(fd, 'w') as out:
        json.dump({'camera_id': self.camera_id, 'stream_session_id': self.session_id,
                   'scope': 'Intentional one-shot native camera teardown'}, out)
    self.gait._test_teardown_owner = (self.camera_id, self.session_id)
    logger.error('controlled camera exception to exercise gait teardown', extra={
        'event': 'test_injected_gait_teardown', 'camera_id': self.camera_id,
        'stream_session_id': self.session_id})
    raise InjectedGaitTeardown('controlled one-shot gait-enabled camera teardown')


def close(self):
    owner = getattr(self, '_test_teardown_owner', (None, None))
    try:
        result = original_close(self)
    except BaseException as exc:
        logger.error('gait teardown failed', extra={
            'event': 'test_gait_teardown_failed', 'camera_id': owner[0],
            'stream_session_id': owner[1], 'exception_type': type(exc).__name__})
        raise
    logger.info('gait extractor and engine owner released', extra={
        'event': 'test_gait_teardown_completed', 'camera_id': owner[0],
        'stream_session_id': owner[1]})
    return result


V8EventPipeline._scene = scene
DlsGaitExtractor.close = close
