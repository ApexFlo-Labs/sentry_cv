"""Segmented observer evidence must remain visible after monitoring renewals."""
import gzip
import json
from pathlib import Path
import subprocess
import sys


def test_original_and_renewed_hardware_segments_are_both_included(tmp_path):
    root = tmp_path/'remote-artifacts'
    case = root/'runs/example/case/raw-example'
    case.mkdir(parents=True)
    (case/'report.json').write_text(json.dumps({'passed': True,
        'steady_started_monotonic': 100, 'steady_ended_monotonic': 110}))
    for directory, timestamp, busy in [('observations', 102, 20), ('observations-20261112', 106, 80)]:
        source = root/directory/'gpu-1/samples.jsonl.gz'
        source.parent.mkdir(parents=True)
        with gzip.open(source, 'wt') as stream:
            stream.write(json.dumps({'received_monotonic': timestamp, 'sample': {
                'period': {'duration': 1000}, 'engines': {'Compute': {'busy': busy}}}})+'\n')
    out = tmp_path/'analysis.json'
    subprocess.run([sys.executable, str(Path(__file__).with_name('analyze_observed_engines.py')),
                    str(tmp_path), '--output', str(out)], check=True, capture_output=True)
    result = json.loads(out.read_text())['cases']['case/raw-example']['metrics']['GPU_engine_Compute_busy_pct']
    assert result['samples'] == 2
    assert result['min'] == 20 and result['max'] == 80 and result['p50'] == 50
