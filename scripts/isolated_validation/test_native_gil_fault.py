import copy

import pytest

from scripts.isolated_validation.v8_recovery_image_validation import (
    gil_blocking_fault_script,
    gil_stall_recovery_verified,
)


def test_gil_fault_uses_pydll_and_preserves_injection_guard():
    script = ('guard_once = True\n'
              'event = "test_injected_native_stall"\n'
              'while True:time.sleep(1)\n')
    changed = gil_blocking_fault_script(script)
    assert 'ctypes.PyDLL(None).sleep(600)' in changed
    assert 'guard_once = True' in changed
    assert 'test_injected_native_gil_stall' in changed
    assert 'while True:time.sleep(1)' not in changed
    compile(changed, '<native-gil-fault>', 'exec')


@pytest.mark.parametrize('script', ['', 'while True:time.sleep(1)\nwhile True:time.sleep(1)'])
def test_unknown_or_ambiguous_fault_script_is_refused(script):
    with pytest.raises(ValueError):
        gil_blocking_fault_script(script)


@pytest.mark.parametrize('failure', [None, 'no-injection', 'same-process', 'no-kill',
                                    'wrong-worker', 'boolean-pid', 'duplicate-injection'])
def test_external_parent_gil_recovery_requires_real_fault_and_reaping(failure):
    events = [
        {'event': 'test_injected_native_gil_stall', 'pid': 51},
        {'event': 'native_worker_watchdog_restart', 'pid': 9, 'camera_process_pid': 51},
        {'event': 'camera_process_exited', 'pid': 9, 'camera_process_pid': 51, 'exit_code': -9},
    ]
    if failure == 'no-injection':
        events = events[1:]
    elif failure == 'same-process':
        events[1]['pid'] = 51
    elif failure == 'no-kill':
        events[2]['exit_code'] = 0
    elif failure == 'wrong-worker':
        events[2]['camera_process_pid'] = 99
    elif failure == 'boolean-pid':
        events[0]['pid'] = True
    elif failure == 'duplicate-injection':
        events.append(copy.deepcopy(events[0]))
    assert gil_stall_recovery_verified(events) is (failure is None)
