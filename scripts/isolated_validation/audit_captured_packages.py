#!/usr/bin/env python3
"""Offline audit of completed capture-agent records and saved JPEG subsets.

The receiver checks actual vectors/multipart bytes on every request. Its record
retains dimensions and calculated norm, not vectors. This audit checks recorded
links and independently decodes saved JPEGs; it cannot verify subject identity.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from PIL import Image


DIMENSIONS = {'adaface-ir101-v18.1': 512, 'transreid-ssl-v18.1': 384,
              'gaitbase-v18.1': 4096, 'siglip2-base-v1': 768}


def audit(path):
    counts = Counter()
    errors = []
    identifiers = set()
    sequences = []

    def check_record(record):
        oid = record['observation_id']
        evidence = record.get('evidence_metadata', [])
        embeddings = record.get('embedding_metadata', [])
        assert set(record.get('evidence_ids', [])) == {e['evidence_id'] for e in evidence}
        assert set(record.get('embedding_ids', [])) == {e['embedding_id'] for e in embeddings}
        for e in evidence:
            assert e['observation_id'] == oid, 'evidence parent'
            frame = e.get('source_frame_id')
            if frame:
                assert frame.startswith(record['camera_id']+':'), 'evidence camera'
            box = e.get('source_bbox_normalized')
            if box:
                assert 0 <= box['x1'] < box['x2'] <= 1, 'crop x bounds'
                assert 0 <= box['y1'] < box['y2'] <= 1, 'crop y bounds'
        for e in embeddings:
            assert e['observation_id'] == oid, 'embedding parent'
            assert e['evidence_id'] in record.get('evidence_ids', []), 'embedding evidence'
            assert e['dims'] == DIMENSIONS[e['profile_id']], 'embedding dimensions'
            assert math.isfinite(e['l2_norm']) and abs(e['l2_norm']-1) < .01, 'embedding norm'
            if e.get('presence_id'):
                assert e['presence_id'] == record.get('presence_id'), 'embedding presence'
            if e.get('source_frame_id'):
                assert e['source_frame_id'] == record.get('source_frame_id'), 'embedding frame'

    with (path/'packages.jsonl').open() as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
                check_record(record)
                assert record['package_id'] not in identifiers, 'duplicate accepted package ID'
                identifiers.add(record['package_id'])
                sequences.extend(s['sequence'] for s in record['sequences'])
                counts['packages'] += 1
                counts['evidence_metadata'] += len(record.get('evidence_metadata', []))
                counts['embedding_metadata'] += len(record.get('embedding_metadata', []))
                counts['event:'+record['event_type']] += 1
            except Exception as exc:
                errors.append({'line': line_number, 'error': str(exc) or type(exc).__name__})
    if sequences and sorted(sequences) != list(range(1, len(sequences)+1)):
        errors.append({'error': 'captured acceptance sequences are not unique/contiguous from one'})
    for metadata_path in sorted((path/'gallery').glob('*/metadata.json')):
        try:
            record = json.loads(metadata_path.read_text())
            check_record(record)
            for item in record.get('evidence_metadata', []):
                image_path = metadata_path.parent/(item['part_name']+'.jpg')
                body = image_path.read_bytes()
                assert len(body) == item['size_bytes'], 'JPEG byte size'
                assert 'sha256:'+hashlib.sha256(body).hexdigest() == item['sha256'], 'JPEG SHA'
                with Image.open(image_path) as image:
                    assert image.format == 'JPEG', 'JPEG format'
                    assert image.size == (item['width'], item['height']), 'JPEG dimensions'
                    image.load()
                counts['JPEGs_independently_decoded'] += 1
            counts['sampled_gallery_packages'] += 1
        except Exception as exc:
            errors.append({'file': str(metadata_path), 'error': str(exc) or type(exc).__name__})
    return {'path': str(path), 'passed': bool(counts['packages'] and not errors),
            'counts': dict(counts), 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = []
    for report in sorted(args.root.glob('runs/*/*/raw-*/report.json')):
        if (report.parent/'packages.jsonl').exists():
            results.append(audit(report.parent))
    result = {'scope': 'Recorded package/metadata links and independent saved-JPEG integrity only; '
              'not recognition accuracy, distinct-camera pixel provenance, or real-agent durability.',
              'passed': bool(results and all(r['passed'] for r in results)), 'cases': results}
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'passed': result['passed'], 'cases': len(results),
                      'packages': sum(r['counts'].get('packages', 0) for r in results),
                      'JPEGs': sum(r['counts'].get('JPEGs_independently_decoded', 0) for r in results),
                      'errors': sum(len(r['errors']) for r in results)}))
    raise SystemExit(0 if result['passed'] else 1)
