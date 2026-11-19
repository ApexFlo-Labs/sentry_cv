"""Offline tests of evidence auditing, not runtime/native regression execution."""
import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).with_name('check_regression_gate.py')
SPEC = importlib.util.spec_from_file_location('regression_gate', MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
IMAGE = 'a' * 64


class EvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.specs = [{'id': 'INC-001', 'required_assertions': ['fresh_frames', 'peer_progress'],
                       'required_evidence_levels': ['unit', 'native']}]
        self.report = gate.template(self.specs, IMAGE)
        row = self.report['results'][0]
        row.update(status='PASS', run_id='fixture-001', executed_at='2026-11-06T08:00:00+00:00',
                   command='python3 controlled_fixture.py',
                   assertions_passed=['fresh_frames', 'peer_progress'])
        for level in ['unit', 'native']:
            path = self.root / (level + '.json')
            path.write_text(json.dumps({'fixture': level, 'assertions': row['assertions_passed']}))
            row['evidence'].append({'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                    'level': level})

    def check(self, report=None):
        return gate.check_results(self.specs, self.report if report is None else report, IMAGE, self.root)

    def test_complete_same_image_evidence_passes(self):
        self.assertTrue(self.check()['qualified'])

    def test_new_template_never_claims_execution(self):
        report = gate.template(self.specs, IMAGE)
        self.assertEqual(report['results'][0]['status'], 'NOT_RUN')
        self.assertEqual(self.check(report)['status'], 'BLOCKED')

    def test_previous_image_pass_cannot_be_reused(self):
        for field in ['header', 'case']:
            with self.subTest(field=field):
                report = copy.deepcopy(self.report)
                target = report if field == 'header' else report['results'][0]
                target['image_config_id'] = 'b' * 64
                self.assertFalse(self.check(report)['qualified'])

    def test_missing_duplicate_unknown_case_blocks(self):
        for rows in [[], self.report['results'] * 2,
                     self.report['results'] + [{'id': 'INC-999'}]]:
            with self.subTest(rows=len(rows)):
                report = copy.deepcopy(self.report)
                report['results'] = rows
                self.assertFalse(self.check(report)['qualified'])

    def test_failed_blocked_or_invalid_setup_is_not_pass(self):
        for status in ['FAIL', 'BLOCKED', 'NOT_RUN', 'INVALID_TEST_SETUP', 'PARTIAL']:
            with self.subTest(status=status):
                self.report['results'][0]['status'] = status
                result = self.check()
                self.assertFalse(result['qualified'])
                self.assertEqual(result['status'], 'FAIL' if status == 'FAIL' else 'BLOCKED')

    def test_unit_only_cannot_close_native_requirement(self):
        self.report['results'][0]['evidence'].pop()
        self.assertFalse(self.check()['qualified'])

    def test_missing_assertion_blocks_even_with_pass_label(self):
        self.report['results'][0]['assertions_passed'].remove('peer_progress')
        self.assertFalse(self.check()['qualified'])

    def test_missing_and_tampered_evidence_blocks(self):
        path = self.root / 'native.json'
        path.write_text('changed after execution')
        self.assertFalse(self.check()['qualified'])
        path.unlink()
        self.assertFalse(self.check()['qualified'])

    def test_missing_run_metadata_or_timezone_blocks(self):
        for field, value in [('command', ''), ('run_id', ''),
                             ('executed_at', '2026-11-06T08:00:00'),
                             ('executed_at', 'not-a-time')]:
            with self.subTest(field=field, value=value):
                report = copy.deepcopy(self.report)
                report['results'][0][field] = value
                self.assertFalse(self.check(report)['qualified'])

    def test_malformed_inputs_do_not_qualify(self):
        for report in [[], {'results': None}, {'results': [None]},
                       {'image_config_id': IMAGE, 'results': []}]:
            with self.subTest(report=report):
                self.assertFalse(self.check(report)['qualified'])
        self.report['results'][0].update(assertions_passed=None, evidence=[None])
        self.assertFalse(self.check()['qualified'])

    def test_empty_registry_does_not_pass_vacuously(self):
        self.assertFalse(gate.check_results([], {'image_config_id': IMAGE}, IMAGE, self.root)['qualified'])

    def test_only_full_digests_accepted(self):
        self.assertEqual(gate.config_id('sha256:' + IMAGE), IMAGE)
        for value in ['latest', 'v8-r2', IMAGE[:12], '', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.config_id(value)


class RepositoryRegistryTests(unittest.TestCase):
    def test_links_and_incident_membership_are_consistent(self):
        matrix = json.loads(gate.MATRIX.read_text())
        cases = {c['id']: c for c in matrix['cases']}
        incidents = {r['id']: r for r in matrix['incident_regressions']}
        self.assertEqual(len(cases), matrix['metadata']['case_count'])
        self.assertEqual(len(incidents), matrix['metadata']['incident_regression_count'])
        for key, spec in incidents.items():
            self.assertEqual(spec['new_image_status'], 'NOT_RUN')
            self.assertTrue(spec['required_assertions'])
            self.assertTrue(spec['required_evidence_levels'])
            for path in spec['saved_tests']:
                self.assertTrue((gate.ROOT / path.split('::')[0]).is_file(), path)
            for case_id in spec['case_ids']:
                self.assertIn(key, cases[case_id]['incident_regression_ids'])

    def test_historical_ledger_verdicts_not_reset(self):
        # Canonical original fields from published checkpoint b645c04. Keep the
        # test runnable in a shallow clone without fetching historical objects.
        # Future intentional ledger edits must update this snapshot explicitly.
        with (gate.ROOT / 'PIPELINE_TEST_LEDGER.csv').open(newline='') as handle:
            current = list(csv.DictReader(handle))
        original_fields = [{k: v for k, v in row.items() if k != 'incident_regression_ids'}
                           for row in current]
        digest = hashlib.sha256(json.dumps(original_fields, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(len(current), 282)
        self.assertEqual(digest, '1cfd902efad0ad018ce8a54e8de241b437fcb89ca72013b0cb9b94682e35de4e')


if __name__ == '__main__':
    unittest.main()
