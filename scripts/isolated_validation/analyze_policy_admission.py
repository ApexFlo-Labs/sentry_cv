"""Verify real native ANPR results survive an explicit scheduler refusal.

Matches the refused logical observation/evidence to both an admitted retry and
the schema/hash-checking agent capture. Outer package IDs may change before
admission; observation/evidence IDs, original timestamps and bytes must not.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def analyze(root, expected_image):
    report = json.loads((root / 'report.json').read_text())
    events = []
    for line in (root / 'container.log').read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    captured = [json.loads(line) for line in (root / 'packages.jsonl').read_text().splitlines() if line]
    checks = []
    for kind in ['plate_read', 'plate_outcome']:
        refused = [event for event in events
                   if event.get('event') == 'validation_policy_admission_refused'
                   and event.get('event_type') == kind]
        row = {'kind': kind, 'injected_refusals': len(refused), 'passed': False}
        if len(refused) != 1:
            row['reason'] = 'Expected exactly one real refused observation; missing work is not a pass'
            checks.append(row)
            continue
        first = refused[0]
        admitted = [event for event in events
                    if event.get('event') == 'validation_policy_admission_retry'
                    and event.get('observation_id') == first['observation_id']
                    and event.get('matches_initial_logical_payload') is True
                    and event.get('logical_payload_digest') == first['logical_payload_digest']]
        accepted = [package for package in captured if package.get('observation_id') == first['observation_id']]
        row.update(observation_id=first['observation_id'], source_frame_id=first.get('source_frame_id'),
                   observed_at=first['observed_at'], matching_admissions=len(admitted),
                   matching_agent_packages=len(accepted))
        if len(admitted) != 1 or len(accepted) != 1:
            row['reason'] = 'Refused source observation did not reach both real admission and agent capture exactly once'
            checks.append(row)
            continue
        package = accepted[0]
        fields_match = all(package.get(field) == first.get(field) for field in (
            'camera_id', 'event_type', 'presence_id', 'source_frame_id', 'observed_at',
        ))
        fingerprints = [{field: item.get(field) for field in (
            'evidence_id', 'sha256', 'size_bytes', 'source_frame_id', 'captured_at',
        )} for item in package.get('evidence_metadata', [])]
        row.update(fields_match=fields_match,
                   evidence_match=fingerprints == first.get('evidence_fingerprints'),
                   frame_evidence_present='frame' in package.get('evidence', []))
        row['passed'] = bool(fields_match and row['evidence_match'] and row['frame_evidence_present'])
        checks.append(row)
    prerequisites = {
        'exact_image': report.get('image_config_id') == expected_image,
        'explicit_diagnostic': report.get('diagnostic_hook_injected') is True,
        'probe_passed': report.get('passed') is True,
        'no_fatal_lines': not report.get('fatal_lines'),
        'source_utc_sanity': report.get('source_utc_sanity', {}).get('passed') is True,
    }
    return {'utc': datetime.now(timezone.utc).isoformat(), 'source_report': str(root / 'report.json'),
            'image': expected_image, 'scope': __doc__, 'prerequisites': prerequisites,
            'checks': checks, 'passed': all(prerequisites.values()) and all(row['passed'] for row in checks)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.root, args.image)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)
