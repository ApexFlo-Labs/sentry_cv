"""Explicit native INC-010 fault: refuse the first real plate package per kind.

Used only through --diagnostic-hook. Does not change pixels, model outputs,
selection/quality gates, or accepted payloads. Production _admit handles every
subsequent request. Logical payload matching excludes the outer package_id,
which the existing CV client allocates for each local admission attempt.
"""


def install():
    import logging
    from pathlib import Path
    import sys
    worker = Path('/opt/traffic-pilot/services/worker')
    if worker.is_dir():
        sys.path.insert(0, str(worker))
    from pipeline import sentinel_v8_handoff as handoff

    original = handoff.V8HandoffScheduler._admit
    refused = {}
    log = logging.getLogger('validation.policy_admission')

    def identity(request):
        manifest = dict(request['manifest'])
        package_id = manifest.pop('package_id')
        observation = manifest['observation']
        return {
            'camera_id': request['camera_id'],
            'event_type': observation['event_type'],
            'observation_id': observation['observation_id'],
            'presence_id': observation.get('presence_id'),
            'source_frame_id': observation.get('source_frame_id'),
            'observed_at': observation['observed_at'],
            'package_id': package_id,
            'logical_payload_digest': handoff.package_digest(manifest, request['evidence_parts']),
            'evidence_fingerprints': [{
                field: item['metadata'].get(field) for field in (
                    'evidence_id', 'sha256', 'size_bytes', 'source_frame_id', 'captured_at',
                )
            } for item in manifest['evidence']],
        }

    def admit(self, request):
        observation = request['manifest']['observation']
        kind = observation.get('event_type')
        if request['camera_id'] != 'capacity-01' or kind not in {'plate_read', 'plate_outcome'}:
            return original(self, request)
        key = (request['camera_id'], kind)
        current = identity(request)
        if key not in refused:
            refused[key] = {'initial': current, 'verified': False, 'logged_attempts': 0}
            with self.condition:
                self.counters['backpressure'] += 1
                self.last_error = 'validation_injected_one_shot_admission_refusal'
                self._snapshot_locked()
            log.warning('Test injected one-shot scheduler admission refusal', extra={
                'event': 'validation_policy_admission_refused', **current,
            })
            return False, 'delivery_backpressure'

        accepted, reason = original(self, request)
        tracked = refused[key]
        if accepted and not tracked['verified']:
            initial = tracked['initial']
            matches = (current['observation_id'] == initial['observation_id']
                       and current['logical_payload_digest'] == initial['logical_payload_digest'])
            if matches or tracked['logged_attempts'] < 10:
                log.info('Native policy result after the injected local refusal', extra={
                    'event': 'validation_policy_admission_retry', **current,
                    'initial_observation_id': initial['observation_id'],
                    'matches_initial_logical_payload': matches,
                })
                tracked['logged_attempts'] += 1
            tracked['verified'] = matches
        return accepted, reason

    handoff.V8HandoffScheduler._admit = admit


if __name__ == 'sitecustomize':
    install()
