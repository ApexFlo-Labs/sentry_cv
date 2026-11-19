#!/usr/bin/env python3
"""Fail-closed audit of incident regression evidence for one built image.

This does not execute behavioral tests or prove an arbitrary report truthful.
It prevents missing results, stale-image passes and missing/altered evidence
from being silently accepted by the documented release review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / 'PIPELINE_TEST_MATRIX.json'


def config_id(value):
    value = str(value or '').removeprefix('sha256:').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('a full 64-character image configuration digest is required')
    return value


def check_results(regressions, results, expected_image_id, evidence_root):
    expected = config_id(expected_image_id)
    problems = []
    if not isinstance(results, dict):
        results = {}
        problems.append('result document must be an object')
    try:
        if config_id(results.get('image_config_id')) != expected:
            problems.append('result document belongs to another image')
    except ValueError:
        problems.append('result document has no valid image configuration digest')
    by_id = {}
    known = {r['id'] for r in regressions}
    supplied = results.get('results', [])
    if not isinstance(supplied, list):
        supplied = []
        problems.append('results must be a list')
    for result in supplied:
        if not isinstance(result, dict):
            problems.append('case result must be an object')
            continue
        key = result.get('id')
        if key not in known:
            problems.append('unknown incident result ID')
        elif key in by_id:
            problems.append('duplicate result: ' + key)
        else:
            by_id[key] = result
    rows = []
    for spec in regressions:
        key = spec['id']
        row = by_id.get(key, {})
        errors = []
        status = row.get('status', 'NOT_RUN')
        if status != 'PASS':
            errors.append('fresh PASS missing: ' + str(status))
        try:
            if config_id(row.get('image_config_id')) != expected:
                errors.append('case belongs to another image')
        except ValueError:
            errors.append('case image digest missing')
        if not row.get('run_id') or not row.get('command'):
            errors.append('run ID or executed command missing')
        try:
            executed = datetime.fromisoformat(str(row.get('executed_at', '')).replace('Z', '+00:00'))
            if executed.tzinfo is None:
                raise ValueError('timezone missing')
        except ValueError:
            errors.append('timezone-aware execution timestamp missing')
        assertions_raw = row.get('assertions_passed', [])
        assertions = {a for a in assertions_raw if isinstance(a, str)} if isinstance(assertions_raw, list) else set()
        if not set(spec['required_assertions']).issubset(assertions):
            errors.append('required behavioral assertions missing')
        levels = set()
        entries = row.get('evidence', [])
        if not isinstance(entries, list):
            entries = []
            errors.append('evidence must be a list')
        for evidence in entries:
            if not isinstance(evidence, dict):
                errors.append('evidence entry must be an object')
                continue
            path = evidence.get('path')
            if not isinstance(path, str) or not path:
                errors.append('evidence path missing')
                continue
            path = Path(path)
            path = path if path.is_absolute() else Path(evidence_root) / path
            try:
                expected_hash = config_id(evidence.get('sha256'))
                with path.open('rb') as handle:
                    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
            except (OSError, ValueError):
                errors.append('evidence unavailable or hash invalid')
                continue
            if actual != expected_hash:
                errors.append('evidence changed after execution')
                continue
            if isinstance(evidence.get('level'), str):
                levels.add(evidence['level'])
        if not set(spec['required_evidence_levels']).issubset(levels):
            errors.append('required evidence levels missing')
        rows.append({'id': key, 'status': ('FAIL' if status == 'FAIL' else 'BLOCKED') if errors else 'PASS',
                     'reasons': errors})
    passed = bool(rows) and not problems and all(r['status'] == 'PASS' for r in rows)
    return {'image_config_id': expected, 'status': 'PASS' if passed else 'FAIL' if any(r['status'] == 'FAIL' for r in rows) else 'BLOCKED',
            'qualified': passed, 'global_errors': problems, 'cases': rows,
            'note': 'Evidence completeness/provenance gate; behavioral truth depends on trusted fixtures and review. Not a CI/registry permission control.'}


def template(regressions, image_id):
    image_id = config_id(image_id)
    return {'image_config_id': image_id, 'created_at': datetime.now(timezone.utc).isoformat(),
            'note': 'No tests were run by creating this document. Never copy PASS from a different image.',
            'results': [{'id': r['id'], 'image_config_id': image_id, 'status': 'NOT_RUN',
                         'run_id': '', 'executed_at': '', 'command': '',
                         'assertions_passed': [], 'evidence': [], 'failure_or_block_reason': ''}
                        for r in regressions]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='locally present candidate image; inspected, never rebuilt')
    parser.add_argument('--matrix', type=Path, default=MATRIX)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--init', type=Path, help='create a new all-NOT_RUN checklist; refuses overwrite')
    choice.add_argument('--check', type=Path, help='audit completed results; does not run tests')
    parser.add_argument('--summary', type=Path)
    args = parser.parse_args()
    regressions = json.loads(args.matrix.read_text())['incident_regressions']
    image = json.loads(subprocess.check_output(['podman', 'image', 'inspect', args.image], text=True))[0]
    identity = config_id(image['Id'])
    if args.init:
        args.init.parent.mkdir(parents=True, exist_ok=True)
        with args.init.open('x') as f:
            json.dump(template(regressions, identity), f, indent=2)
            f.write('\n')
        print(f'Created {len(regressions)} NOT_RUN incident checks; no behavioral tests executed.')
        return 0
    report = check_results(regressions, json.loads(args.check.read_text()), identity, args.check.parent)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'qualified': report['qualified'],
                      'pass': sum(r['status'] == 'PASS' for r in report['cases']),
                      'not_passed': sum(r['status'] != 'PASS' for r in report['cases']),
                      'global_errors': report['global_errors']}))
    return 0 if report['qualified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
