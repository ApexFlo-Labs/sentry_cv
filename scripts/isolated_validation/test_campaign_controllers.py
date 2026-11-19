"""Controller/guard integration regressions; no native workload is started."""
import copy
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location('finish_campaign',
    Path(__file__).with_name('finish_remote_campaign.py'))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


@pytest.mark.parametrize('status,exit_code', [('PASS', 0), ('FAIL', 1)])
def test_accepts_actual_terminal_guard_status_format(status, exit_code):
    # run_guarded_case emits top-level "status"; "result" is per-attempt.
    verdict = {'status': status, 'attempt': 2, 'supervisor_exit_code': exit_code,
               'attempts': [{'attempt': 1, 'result': 'THERMAL_EXCLUDED', 'cleanup_ok': True},
                            {'attempt': 2, 'result': status, 'cleanup_ok': True}]}
    assert controller.terminal_guard_attempt(verdict) == 2


@pytest.mark.parametrize('fault', ['running', 'excluded', 'no_exit', 'no_attempt',
    'bool_attempt', 'no_cleanup', 'mismatch', 'duplicate', 'legacy_result_only'])
def test_partial_or_unclean_guard_cannot_be_used_as_test_result(fault):
    verdict = {'status': 'PASS', 'attempt': 1, 'supervisor_exit_code': 0,
               'attempts': [{'attempt': 1, 'result': 'PASS', 'cleanup_ok': True}]}
    if fault == 'running': verdict['status'] = 'RUNNING'
    if fault == 'excluded': verdict['status'] = 'THERMAL_EXCLUDED'
    if fault == 'no_exit': verdict.pop('supervisor_exit_code')
    if fault == 'no_attempt': verdict['attempts'] = []
    if fault == 'bool_attempt': verdict['attempt'] = True
    if fault == 'no_cleanup': verdict['attempts'][0]['cleanup_ok'] = False
    if fault == 'mismatch': verdict['attempts'][0]['result'] = 'FAIL'
    if fault == 'duplicate': verdict['attempts'].append(copy.deepcopy(verdict['attempts'][0]))
    if fault == 'legacy_result_only': verdict['result'] = verdict.pop('status')
    with pytest.raises(AssertionError):
        controller.terminal_guard_attempt(verdict)
