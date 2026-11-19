import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from summarize_remote_campaign import case_records


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def case(root, name, guard, report=None):
    folder = root/'runs'/'test'/name
    store(folder/'status.json', guard)
    if report is not None:
        store(folder/'raw-run'/'report.json', report)


def test_live_followup_is_not_a_finished_result(tmp_path):
    case(tmp_path, 'live', {'status': 'RUNNING'}, {'passed': True})
    assert case_records(tmp_path, 'test') == []


def test_main_ledger_failure_is_not_overridden_by_duplicate_report(tmp_path):
    store(tmp_path/'continuation-ledger.json', [{'case':'old', 'status':'FAIL'}])
    case(tmp_path, 'old', {'status':'PASS', 'supervisor_exit_code':0}, {'passed':True})
    rows = case_records(tmp_path, 'test')
    assert len(rows) == 1 and rows[0]['status'] == 'FAIL'


def test_closed_native_followup_uses_both_report_and_guard(tmp_path):
    case(tmp_path, 'done', {'status':'PASS', 'supervisor_exit_code':0}, {'passed':True})
    assert case_records(tmp_path, 'test')[0]['status'] == 'PASS'


def test_resource_excluded_probe_never_becomes_pass(tmp_path):
    case(tmp_path, 'hot', {'status':'BLOCKED_RESOURCE_PRESSURE', 'supervisor_exit_code':2}, {'passed':True})
    assert case_records(tmp_path, 'test')[0]['status'] == 'BLOCKED_RESOURCE_PRESSURE'


def test_diagnostic_pass_is_not_native_capacity_pass(tmp_path):
    case(tmp_path, 'trace', {'status':'PASS', 'supervisor_exit_code':0},
         {'passed':True, 'diagnostic_fire_input_capture_injected':True})
    assert case_records(tmp_path, 'test')[0]['status'] == 'DIAGNOSTIC_REQUIRES_REVIEW'


def test_guard_without_behavioral_report_needs_review(tmp_path):
    case(tmp_path, 'offline', {'status':'PASS', 'supervisor_exit_code':0})
    assert case_records(tmp_path, 'test')[0]['status'] == 'FUNCTIONAL_REPORT_REQUIRED'
