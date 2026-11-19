"""Self-check one-shot fault isolation before injecting it into a native image."""
from pathlib import Path
import types
from unittest.mock import patch

import pytest


def test_fault_is_one_shot_scoped_and_audits_actual_close(tmp_path):
    class Gait:
        def __init__(self):
            self.closed = 0
        def close(self):
            self.closed += 1
            return 'closed'
    class Pipeline:
        def __init__(self, camera):
            self.camera_id, self.session_id = camera, 'session'
            self.gait = Gait()
        def _scene(self, *a, **kw):
            return 'unchanged-return'
    parent = types.ModuleType('pipeline')
    secondary, events = types.ModuleType('pipeline.dls_secondary'), types.ModuleType('pipeline.v8_events')
    secondary.DlsGaitExtractor, events.V8EventPipeline = Gait, Pipeline
    source = Path(__file__).with_name('gait_teardown_fault_hook.py')
    namespace = {'__name__': 'fault_fixture_test'}
    with patch.dict('sys.modules', {'pipeline': parent, 'pipeline.dls_secondary': secondary,
                                    'pipeline.v8_events': events}):
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
    namespace['MARKER'] = tmp_path/'once'
    first, peer = Pipeline('capacity-01'), Pipeline('capacity-02')
    with patch.object(namespace['time'], 'monotonic', return_value=1):
        assert first._scene() == 'unchanged-return'
    with patch.object(namespace['time'], 'monotonic', return_value=22):
        assert peer._scene() == 'unchanged-return'
        with pytest.raises(namespace['InjectedGaitTeardown']):
            first._scene()
        assert first._scene() == 'unchanged-return'
    assert first.gait.close() == 'closed' and first.gait.closed == 1
    assert peer.gait.closed == 0
