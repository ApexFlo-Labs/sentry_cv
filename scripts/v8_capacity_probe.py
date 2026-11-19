#!/usr/bin/env python3
"""Run a bounded multi-camera capacity qualification against a V8 image."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "newdetails" / "sentry-v8"
AGENT = ROOT / "scripts" / "isolated_validation" / "v8_capture_agent.py"
SOURCE_URLS = [f"tcp://192.168.1.92:{port}" for port in range(9001, 9011)]


def canonical(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def source_urls_reused(urls) -> bool:
    """Exact URI reuse, not a claim that recordings show different scenes."""
    return len(set(urls)) < len(urls)


def startup_quota_release_command(engine: str, container_id: str,
                                  capacity_cores: int, period_us: int | None) -> list[str]:
    # Docker rejects NanoCPUs (--cpus) when the container already has explicit
    # CpuPeriod/CpuQuota. Restore using the same representation used at launch.
    if period_us is not None:
        return [engine, 'update', '--cpu-period', str(period_us),
                '--cpu-quota', str(capacity_cores * period_us), container_id]
    return [engine, 'update', '--cpus', str(capacity_cores), container_id]


def stage_compilation_cache(seed: Path, image_id: str, output: Path) -> list[str]:
    """Private same-image/same-boot compiler cache, never camera state."""
    seed = Path(seed).resolve()
    manifest = json.loads((seed/'CACHE_SEED_MANIFEST.json').read_text())
    boot = hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
    if (manifest['image_config_id'] != image_id.removeprefix('sha256:')
            or manifest['host'] != os.uname().nodename
            or manifest['kernel'] != os.uname().release
            or manifest['host_boot_id_sha256'] != boot):
        raise ValueError('compiler cache fixture must match exact image, host and boot')
    for relative, expected in manifest['files'].items():
        source = seed/relative
        if (source.is_symlink() or not source.resolve().is_relative_to(seed)
                or Path(relative).parts[0] not in ('npu', 'ov')
                or not source.is_file() or source.stat().st_size != expected['size_bytes']
                or file_sha256(source) != expected['sha256']):
            raise ValueError('compiler cache seed differs from its frozen manifest')
    destination = output/'compilation-cache'
    destination.mkdir(mode=0o700)
    mounts = []
    for part, target in [('npu', '/tmp/sentinel/cache/ze_intel_npu_cache'), ('ov', '/tmp/ov_cache')]:
        selected = destination/part
        selected.mkdir(mode=0o777)
        selected.chmod(0o777)
        for relative in manifest['files']:
            path = Path(relative)
            if path.parts[0] != part:
                continue
            copied = destination/path
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed/path, copied)
            copied.chmod(0o666)
        mounts += ['-v', f'{selected}:{target}:rw']
    (output/'compilation-cache-start.json').write_text(json.dumps({
        'condition': 'warm same-image/host compiler cache; NOT cold-start qualification',
        'seed_manifest': manifest, 'seed_manifest_sha256': file_sha256(seed/'CACHE_SEED_MANIFEST.json'),
        'scope': 'Only compiled NPU/model blobs; fresh camera/presence/tracker/policy state'}, indent=2)+'\n')
    return mounts


def validation_phase(phase: str, *, native_clock_deadlines_active: bool = False) -> None:
    target = os.environ.get("SENTINEL_VALIDATION_PHASE_FILE")
    if target:
        path = Path(target)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'phase': phase,
                                         'native_clock_deadlines_active': native_clock_deadlines_active,
                                         'utc': datetime.now(timezone.utc).isoformat(),
                                         'monotonic': time.monotonic()}) + '\n')
        os.replace(temporary, path)


def startup_pause_seconds() -> float:
    target = os.environ.get("SENTINEL_VALIDATION_PAUSE_CLOCK")
    if not target or not Path(target).exists():
        return 0.0
    value = float(json.loads(Path(target).read_text())['total_seconds'])
    if not math.isfinite(value) or value < 0:
        raise ValueError('invalid test startup pause clock')
    return value


def startup_monotonic() -> float:
    # Only test startup/warmup timers exclude declared external cooling. Source
    # timestamps, API acceptance, all raw samples and steady time remain real.
    return time.monotonic() - startup_pause_seconds()


def steady_throughput(samples: list[dict], minimum_ratio: float,
                      window_minimum_ratio: float | None = None) -> dict:
    """Use cumulative counts over measured time, not just final rolling FPS."""
    window_minimum_ratio = minimum_ratio if window_minimum_ratio is None else window_minimum_ratio
    steady = [s for s in samples if s.get("sample_phase") == "steady"]
    result = {"passed": False, "per_camera": {}, "duration_seconds": 0.0}
    if len(steady) < 2:
        return result
    first, last = steady[0], steady[-1]
    duration = last["sample_monotonic_seconds"] - first["sample_monotonic_seconds"]
    cameras = first.get("per_camera", {})
    if not math.isfinite(duration) or duration <= 0 or not cameras or set(cameras) != set(last.get("per_camera", {})):
        return result
    default_target = float(first["target_decode_fps"]) / len(cameras)
    if not math.isfinite(default_target) or default_target <= 0:
        return result
    def counter_time(row, record):
        if record.get('counter_uptime_seconds') is not None:
            return float(record['counter_uptime_seconds'])
        if record.get('telemetry_observed_at'):
            return datetime.fromisoformat(record['telemetry_observed_at'].replace('Z', '+00:00')).timestamp()
        return float(row['sample_monotonic_seconds'])
    for camera, initial in cameras.items():
        target = float(initial.get("target_fps", default_target))
        if not math.isfinite(target) or target <= 0:
            return result
        previous = initial.get("totals", {}).get("decoded_frames")
        previous_time = counter_time(first, initial)
        initial_counter_time = previous_time
        window_ratios = []
        for sample in steady[1:]:
            if set(sample.get("per_camera", {})) != set(cameras):
                return result
            record = sample["per_camera"][camera]
            count = record.get("totals", {}).get("decoded_frames")
            sampled_time = counter_time(sample, record)
            if (previous is None or count is None or count < previous
                    or sampled_time <= previous_time
                    or record.get("target_fps", target) != target):
                return result
            # A restart may rebuild counters past the old value before the last
            # sample. Do not hide it inside an uninterrupted capacity hold.
            for key in ("stream_session_id", "camera_process_pid"):
                if initial.get(key) is not None and record.get(key) != initial[key]:
                    return result
            window_ratios.append((count - previous) / (sampled_time - previous_time) / target)
            previous, previous_time = count, sampled_time
        begin = initial.get("totals", {}).get("decoded_frames")
        end = last["per_camera"][camera].get("totals", {}).get("decoded_frames")
        if begin is None or end is None or end < begin:
            return result
        counter_duration = previous_time - initial_counter_time
        fps = (end - begin) / counter_duration
        result["per_camera"][camera] = {"selected_fps": fps, "ratio": fps / target,
                                        "minimum_window_ratio": min(window_ratios),
                                        "counter_duration_seconds": counter_duration,
                                        "clock": ('reporter_monotonic_uptime' if initial.get('counter_uptime_seconds') is not None
                                                  else 'reporter_utc' if initial.get('telemetry_observed_at') else 'caller_sample_time')}
    result["duration_seconds"] = duration
    result["passed"] = all(c["ratio"] >= minimum_ratio and c["minimum_window_ratio"] >= window_minimum_ratio
                           for c in result["per_camera"].values())
    return result


def steady_completed_work(samples, minimum_ratio, window_minimum_ratio, activity):
    """Count completed model-or-quiet work separately from selected surfaces."""
    completed, inferred = copy.deepcopy(samples), copy.deepcopy(samples)
    for source, model_only in ((completed, False), (inferred, True)):
        for row in source:
            if row.get("sample_phase") != "steady":
                continue
            for record in row.get("per_camera", {}).values():
                totals = record.get("totals", {})
                # RuntimeTelemetry exports a sparse Counter: never-used quiet
                # or inference counters are absent, not unavailable. At least
                # one disposition counter must exist; selected-only telemetry
                # still cannot establish successful completion.
                if not any(key in totals for key in ("inference_frames", "quiet_frames")):
                    return {"passed": False, "reason": "completion counters unavailable"}
                a, b = totals.get("inference_frames", 0), totals.get("quiet_frames", 0)
                totals["decoded_frames"] = a if model_only else a + b
    result = steady_throughput(completed, minimum_ratio, window_minimum_ratio)
    actual = steady_throughput(inferred, minimum_ratio, window_minimum_ratio)
    result["inferred_work"] = actual
    result["quiet_allowed"] = activity != "active"
    result["passed"] = result["passed"] and (activity != "active" or actual["passed"])
    return result


def sanitized_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "REDACTED" if parts.query else "", ""))


def validate_output_oracle(state, expected):
    """Reject impossible/typoed positive fixtures before starting native work."""
    cameras = {c['camera_id']: c for c in state['cameras']}
    if not isinstance(expected, dict) or not set(expected).issubset(cameras):
        raise ValueError('output oracle contains an unknown camera')
    for camera_id, rules in expected.items():
        enabled = {p for b in cameras[camera_id]['bindings']
                   for p in b.get('embedding_profile_ids', [])}
        if not isinstance(rules, dict) or set(rules) - {'events', 'embedding_profiles'}:
            raise ValueError('invalid output oracle rules')
        for kind, required in rules.items():
            if not isinstance(required, (dict, list)):
                raise ValueError('output requirements must be a list or minimum-count mapping')
            if any(not isinstance(key, str) or not key for key in required):
                raise ValueError('invalid output requirement name')
            if isinstance(required, dict) and any(
                    isinstance(n, bool) or not isinstance(n, int) or n < 1
                    for n in required.values()):
                raise ValueError('output minimum counts must be positive integers')
            if kind == 'embedding_profiles' and not set(required).issubset(enabled):
                raise ValueError('output oracle requires a profile not enabled for ' + camera_id)


def available_duration(start, end, intervals):
    """Subtract only explicitly scheduled source-offline intervals, once."""
    spans = []
    for first, last in intervals:
        if last < first:
            raise ValueError('source outage interval is reversed')
        first, last = max(start, first), min(end, last)
        if last > first:
            spans.append((first, last))
    spans.sort()
    merged = []
    for first, last in spans:
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return max(0., end-start-sum(last-first for first, last in merged))


def steady_package_coverage(packages, state, start, end, *, activity="unspecified", expected=None,
                            unavailable_intervals=None):
    """Per-camera steady output coverage, distinct from enabled bindings or accuracy."""
    result = {"passed": True, "per_camera": {}, "activity": activity,
              "positive_workload_oracle_supplied": expected is not None}
    expected = expected or {}
    unavailable_intervals = unavailable_intervals or {}
    for camera in state["cameras"]:
        key = camera["camera_id"]
        records = [p for p in packages if p.get("camera_id") == key
                   and isinstance(p.get("accepted_mono"), (float, int))
                   and start <= p["accepted_mono"] <= end]
        events, profiles = {}, {}
        for p in records:
            event = p.get("event_type")
            events[event] = events.get(event, 0) + 1
            for profile in p.get("embeddings", []):
                profiles[profile] = profiles.get(profile, 0) + 1
        errors = []
        scenes = [p for p in records if p.get("event_type") == "scene_sample"]
        scene_bindings = [b for b in camera["bindings"] if b["app"] == "scene_search"]
        coverage = []
        eligible_seconds = available_duration(start, end, unavailable_intervals.get(key, []))
        for binding in scene_bindings:
            selected = [p for p in scenes if p.get("use_case_id") == binding["use_case_id"]]
            interval = binding.get("sample_interval_seconds", 2) if activity == "active" else 5
            # Selection is "first eligible frame after interval", not an
            # independent exact-rate timer. Account for at most one nominal
            # selected-frame period per sample before judging missing output.
            # This is a test oracle, never a runtime sampling/gate override.
            quantization = 1 / float(camera.get('fps', 8)) if activity == 'active' else 0
            required = max(1, int(eligible_seconds / (interval + quantization)) - 2)
            paired = [p for p in selected if p.get("evidence")
                      and p.get("evidence_metadata") and p.get("embedding_metadata")
                      and "siglip2-base-v1" in p.get("embeddings", [])]
            if len(paired) < required:
                errors.append("missing steady scene image/vector coverage: " + binding["use_case_id"])
            coverage.append({"binding": binding["use_case_id"], "paired_samples": len(paired),
                             "source_available_seconds": eligible_seconds,
                             "minimum_samples": required, "interval_target_seconds": interval,
                             "nominal_frame_quantization_seconds": quantization,
                             "count_oracle_scope": "Frame-eligible target cadence with two boundary samples; not exact wall-period guarantee"})
        required_events = expected.get(key, {}).get("events", [])
        for event in required_events:
            minimum = required_events[event] if isinstance(required_events, dict) else 1
            if events.get(event, 0) < minimum:
                errors.append("expected positive event not exercised: " + event)
        required_profiles = expected.get(key, {}).get("embedding_profiles", [])
        for profile in required_profiles:
            minimum = required_profiles[profile] if isinstance(required_profiles, dict) else 1
            if profiles.get(profile, 0) < minimum:
                errors.append("expected positive embedding not exercised: " + profile)
        if not records:
            errors.append("no timestamped steady packages from this camera")
        result["per_camera"][key] = {"passed": not errors, "errors": errors,
                                     "events": events, "embedding_profiles": profiles,
                                     "scene": coverage}
        result["passed"] = result["passed"] and not errors
    return result


def source_utc_sanity(packages, tolerance=.05):
    """Same-host API causal check, not a substitute for marked capture latency."""
    violations, missing, total = [], 0, 0
    minima = None
    for package in packages:
        try:
            accepted = datetime.fromisoformat(package['accepted_at'].replace('Z', '+00:00'))
            if accepted.tzinfo is None:
                raise ValueError('timezone absent')
        except (KeyError, ValueError, TypeError, AttributeError):
            missing += 1
            continue
        values = [('observation', package.get('observed_at'))]
        values += [('evidence', item.get('captured_at')) for item in package.get('evidence_metadata', [])]
        values += [('embedding', item.get('captured_at')) for item in package.get('embedding_metadata', [])]
        values += [('zone_episode', ep.get(field)) for ep in package.get('zone_episodes', [])
                   for field in ('first_observed_inside_at', 'last_confirmed_inside_at') if ep.get(field)]
        for kind, raw in values:
            try:
                observed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if observed.tzinfo is None:
                    raise ValueError('timezone absent')
                delta = (accepted - observed).total_seconds()
            except (ValueError, TypeError, AttributeError):
                missing += 1
                continue
            total += 1
            minima = delta if minima is None else min(minima, delta)
            if delta < -tolerance:
                violations.append({'kind': kind, 'package_id': package.get('package_id'),
                                   'camera_id': package.get('camera_id'), 'signed_age_seconds': delta})
    return {'passed': total > 0 and not missing and not violations,
            'timestamps_checked': total, 'missing_or_invalid': missing,
            'future_count': len(violations), 'future_examples': violations[:20],
            'minimum_signed_observation_to_api_seconds': minima, 'tolerance_seconds': tolerance,
            'scope': 'Common-host causal UTC sanity only; does not prove actual camera time or end-to-end freshness.'}


def desired_state(
    camera_count: int,
    fps: int,
    *,
    omit_fps: bool = False,
    full_usecases: bool = False,
) -> dict:
    cameras = []
    for index in range(camera_count):
        number = index + 1
        camera_id = f"capacity-{number:02d}"
        zone_id = f"zone-{number}"
        camera = {
            "camera_id": camera_id,
            "source": f"file:/run/secrets/sentinel/cameras/{camera_id}.url",
            "geometry": {
                "zones": [{
                    "id": zone_id,
                    "name": "Full",
                    "poly": [[0.01, 0.01], [0.99, 0.01],
                             [0.99, 0.99], [0.01, 0.99]],
                }],
                "counting_lines": ([{
                    "id": f"line-{number}",
                    "name": "Middle",
                    "a": [0.05, 0.5],
                    "b": [0.95, 0.5],
                    "direction_mapping": {
                        "left_to_right": "out",
                        "right_to_left": "in",
                    },
                }] if full_usecases else []),
            },
            "bindings": [
                {
                    "use_case_id": f"count-{number}",
                    "app": "vehicle_counting",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"anpr-{number}",
                    "app": "anpr",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
            ],
        }
        if full_usecases:
            camera["bindings"].extend([
                {
                    "use_case_id": f"people-count-{number}",
                    "app": "people_counting",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": ["person"],
                },
                {
                    "use_case_id": f"vehicle-presence-{number}",
                    "app": "vehicle_presence",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"person-presence-{number}",
                    "app": "person_presence",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["person"],
                },
                {
                    "use_case_id": f"crossing-{number}",
                    "app": "vehicle_entry_exit_counts",
                    "scope": "geometry",
                    "geometry_ids": [f"line-{number}"],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"people-crossing-{number}",
                    "app": "people_entry_exit_counts",
                    "scope": "geometry",
                    "geometry_ids": [f"line-{number}"],
                    "object_types": ["person"],
                },
                {
                    "use_case_id": f"line-{number}",
                    "app": "line_crossing",
                    "scope": "geometry",
                    "geometry_ids": [f"line-{number}"],
                    "object_types": ["person", "vehicle"],
                },
                {
                    "use_case_id": f"identity-{number}",
                    "app": "person_identity",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": ["person"],
                    "embedding_profile_ids": [
                        "adaface-ir101-v18.1",
                        "transreid-ssl-v18.1",
                        "gaitbase-v18.1",
                    ],
                },
                {
                    "use_case_id": f"scene-{number}",
                    "app": "scene_search",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": [],
                    "embedding_profile_ids": ["siglip2-base-v1"],
                    "sample_interval_seconds": 2,
                    "scene_trigger_use_case_ids": [
                        f"vehicle-presence-{number}",
                        f"person-presence-{number}",
                    ],
                },
                {
                    "use_case_id": f"hazard-{number}",
                    "app": "fire_smoke",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": [],
                },
            ])
        if not omit_fps:
            camera["fps"] = fps
        cameras.append(camera)
    return {
        "contract": "sentry-v8",
        "schema_version": "8.0",
        "edge_id": f"v8-capacity-{camera_count}",
        "deployment_id": f"v8-capacity-{camera_count}",
        "revision": 1,
        "cameras": cameras,
    }


def apply_profiles(state: dict, profiles: list[str]) -> None:
    """Render checked-in workload descriptors, not runtime policy thresholds."""
    definitions = {p["profile_id"]: p for p in json.loads(
        (ROOT / "CAPACITY_TEST_MATRIX.json").read_text())["uniform_profiles"]}
    if len(profiles) != len(state["cameras"]):
        raise ValueError("one declared workload profile is required per camera")
    for camera, name in zip(state["cameras"], profiles):
        if name not in definitions:
            raise ValueError("unknown capacity profile: " + str(name))
        profile = definitions[name]
        camera["bindings"] = [b for b in camera["bindings"] if b["app"] in profile["required_apps"]]
        for binding in camera["bindings"]:
            if binding["app"] == "person_identity":
                binding["embedding_profile_ids"] = profile["identity_embedding_profile_ids"][:]
            if binding["app"] == "scene_search":
                binding["sample_interval_seconds"] = profile["active_scene_interval_seconds"]
                enabled = {b["use_case_id"] for b in camera["bindings"]}
                binding["scene_trigger_use_case_ids"] = [
                    key for key in binding.get("scene_trigger_use_case_ids", []) if key in enabled]
        actual = {b["app"] for b in camera["bindings"]}
        if actual != set(profile["required_apps"]) or "scene_search" not in actual:
            raise ValueError("profile renderer omitted a declared app or scene search")


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def parse_cpu_set(value: str) -> set[int]:
    result = set()
    for part in value.split(','):
        bounds = part.split('-')
        if len(bounds) == 1:
            result.add(int(bounds[0]))
        elif len(bounds) == 2:
            first, last = map(int, bounds)
            if last < first:
                raise ValueError('invalid CPU range')
            result.update(range(first, last + 1))
        else:
            raise ValueError('invalid CPU set')
    if not result or min(result) < 0:
        raise ValueError('invalid CPU set')
    return result


class ProbeResources:
    """Bounded cleanup of exact children/container IDs created by one probe."""
    def __init__(self, engine):
        self.engine = engine
        self.processes = []
        self.handles = []
        self.container_id = None
        self.cidfile = None
        self.errors = []

    def close(self):
        if not self.container_id and self.cidfile and self.cidfile.is_file():
            candidate = self.cidfile.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{12,64}", candidate):
                self.container_id = candidate
        if self.container_id:
            for action in (["stop", "--time", "15"], ["rm", "-f"]):
                try:
                    subprocess.run([self.engine, *action, self.container_id],
                                   capture_output=True, timeout=25)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.errors.append(type(exc).__name__)
            self.container_id = None
            self.cidfile = None
        for process in reversed(self.processes):
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.errors.append(type(exc).__name__)
        self.processes.clear()
        for handle in self.handles:
            try:
                handle.close()
            except OSError as exc:
                self.errors.append(type(exc).__name__)
        self.handles.clear()


def health(port: int) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return json.loads(response.read())
    except Exception:
        return None


def camera_ready(camera: dict) -> bool:
    processing = camera.get("processing") or {}
    totals = processing.get("totals") or {}
    decoder = camera.get("decoder") or {}
    return bool(
        (camera.get("camera_health") or {}).get("state") == "healthy"
        and int(totals.get("decoded_frames") or 0) >= 30
        and decoder.get("last_frame_age_seconds") is not None
        and 0 <= float(decoder["last_frame_age_seconds"]) < 30.0
    )


def summarize(payload: dict, camera_count: int, fps: int,
              targets: dict[str, float] | None = None) -> dict:
    telemetry = payload.get("telemetry") or {}
    cameras = telemetry.get("cameras") or []
    decoded = sum(float((item.get("processing") or {}).get("decoded_fps") or 0)
                  for item in cameras)
    inference = sum(float((item.get("processing") or {}).get("inference_fps") or 0)
                    for item in cameras)
    target = sum(targets.values()) if targets is not None else float(camera_count * fps)
    states: dict[str, int] = {}
    for item in cameras:
        state = str((item.get("camera_health") or {}).get("state") or "missing")
        states[state] = states.get(state, 0) + 1
    scheduler = telemetry.get("handoff_scheduler") or {}
    counters = scheduler.get("counters") or {}
    cgroup = ((telemetry.get("system") or {}).get("container_cgroup") or {})
    processes = telemetry.get("runtime_processes") or {}
    accelerators = telemetry.get("accelerators") or {}
    return {
        "health_observed_at": payload.get("observed_at"),
        "ready": payload.get("ready"),
        "worker_running": payload.get("worker_running"),
        "camera_records": len(cameras),
        "camera_states": states,
        "cameras_with_recent_frames": sum(camera_ready(item) for item in cameras),
        "target_decode_fps": target,
        "aggregate_decode_fps": round(decoded, 3),
        "decode_target_ratio": round(decoded / target, 4) if target else 0.0,
        "aggregate_inference_fps": round(inference, 3),
        "per_camera": {
            str(item.get("camera_id")): {
                "selected_fps": (item.get("processing") or {}).get("decoded_fps"),
                "inference_fps": (item.get("processing") or {}).get("inference_fps"),
                "totals": (item.get("processing") or {}).get("totals", {}),
                "pending_admissions": ((item.get("processing") or {}).get("gauges") or {}).get("dls_pending_admissions"),
                "last_frame_age_seconds": (item.get("decoder") or {}).get("last_frame_age_seconds"),
                "target_fps": (targets or {}).get(str(item.get("camera_id")), fps),
                "stream_session_id": (item.get("decoder") or {}).get("stream_session_id"),
                "camera_process_pid": (item.get("decoder") or {}).get("camera_process_pid"),
                "telemetry_observed_at": item.get("observed_at"),
                "counter_uptime_seconds": item.get("uptime_seconds"),
                "telemetry_fresh": item.get("fresh"),
            } for item in cameras
        },
        "unique_camera_processes": processes.get("unique_camera_processes"),
        "camera_process_rss_mb": processes.get("camera_process_rss_mb_sum"),
        "camera_process_pss_mb": processes.get("camera_process_pss_mb_sum"),
        "container_memory_mb": cgroup.get("memory_current_mb"),
        "container_cpu_cores": cgroup.get("cpu_cores_used"),
        "container_pids": cgroup.get("pids_current"),
        "gpu_utilization_pct": (accelerators.get("GPU") or {}).get(
            "hardware_utilization_pct"
        ),
        "npu_utilization_pct": (accelerators.get("NPU") or {}).get(
            "hardware_utilization_pct"
        ),
        "handoff": counters,
    }


def camera_runtime_snapshot(payload: dict) -> dict[str, dict]:
    """Return the isolation-relevant health fields keyed by camera ID."""
    result = {}
    for camera in ((payload.get("telemetry") or {}).get("cameras") or []):
        camera_id = str(camera.get("camera_id") or "")
        health_record = camera.get("camera_health") or {}
        decoder = camera.get("decoder") or {}
        result[camera_id] = {
            "state": health_record.get("state"),
            "retrying": bool(health_record.get("retrying")),
            "camera_process_pid": decoder.get("camera_process_pid"),
            "counter_uptime_seconds": camera.get("uptime_seconds"),
            "telemetry_fresh": camera.get("fresh"),
            "telemetry_observed_at": camera.get("observed_at"),
            "last_frame_age_seconds": decoder.get("last_frame_age_seconds"),
            "reconnects": int(decoder.get("reconnects") or 0),
            "decoder_errors": int(decoder.get("decoder_errors") or 0),
            "decoded_frames": int((camera.get('processing') or {}).get('totals', {}).get('decoded_frames', 0)),
            "completed_frames": sum(int((camera.get('processing') or {}).get('totals', {}).get(k,0))
                                    for k in ['inference_frames','quiet_frames']),
        }
    return result


def isolation_peers_progress(samples, target_id, targets, minimum_ratio=.9):
    """All peers must stay fresh/ready and progress; cached healthy is insufficient."""
    peers = set(targets)-{target_id}
    if not peers or len(samples)<2:
        return False
    previous = {}
    for sample in samples:
        if not sample.get('ready'):
            return False
        now = sample['sample_monotonic_seconds']
        for camera_id in peers:
            value = sample['cameras'].get(camera_id, {})
            age = value.get('last_frame_age_seconds')
            counter_time = value.get('counter_uptime_seconds')
            if (value.get('state') != 'healthy' or isinstance(age,bool) or not isinstance(age,(float,int))
                    or not 0<=age<4):
                return False
            # Startup history is not a new isolation failure. Require valid
            # cumulative counters and no changes after the healthy baseline;
            # keep pre-baseline errors in the report rather than erasing them.
            error_fields = ('reconnects', 'decoder_errors')
            if any(isinstance(value.get(field), bool)
                   or not isinstance(value.get(field), int)
                   or value[field] < 0 for field in error_fields):
                return False
            if (isinstance(counter_time,bool) or not isinstance(counter_time,(float,int))
                    or not math.isfinite(counter_time) or counter_time<0
                    or value.get('telemetry_fresh') is not True):
                return False
            if camera_id in previous:
                before, then = previous[camera_id]
                duration = counter_time-before['counter_uptime_seconds']
                # Counters and their elapsed interval come from the same
                # reporter snapshot. HTTP poll times can bracket snapshots
                # spanning8 or12 seconds even when polls are10 seconds apart.
                # Freshness and liveness remain independently required above.
                if (value.get('camera_process_pid') != before.get('camera_process_pid') or now<=then
                        or duration<=0
                        or any(value[field] != before[field] for field in error_fields)
                        or any((value[field]-before[field])/duration < targets[camera_id]*minimum_ratio
                               for field in ['decoded_frames','completed_frames'])):
                    return False
            previous[camera_id] = (value,now)
    return True


def run_probe(args: argparse.Namespace) -> tuple[dict, Path]:
    owned = ProbeResources(getattr(args, "engine", "podman"))
    try:
        return _run_probe(args, owned)
    finally:
        owned.close()


def stage_opencl_diagnostic(library: Path, image_record: dict, output: Path, active_only: bool = False) -> list[str]:
    """Freeze an explicitly requested diagnostic ICD; never a runtime default."""
    raw = library.read_bytes()
    if (len(raw) > 16 * 1024 ** 2 or len(raw) < 64
            or raw[:6] != b'\x7fELF\x02\x01'
            or int.from_bytes(raw[16:18], 'little') != 3
            or int.from_bytes(raw[18:20], 'little') != 62):
        raise ValueError('diagnostic ICD must be an amd64 ELF shared library <=16MiB')
    directory = output / 'diagnostic-opencl'
    directory.mkdir()
    (directory / 'libOpenCL.so.1.2').write_bytes(raw)
    for name in ('libOpenCL.so.1', 'libOpenCL.so'):
        (directory / name).symlink_to('libOpenCL.so.1.2')
    inherited = next((v.split('=', 1)[1] for v in image_record.get('Config', {}).get('Env', [])
                      if v.startswith('LD_LIBRARY_PATH=')), '')
    values = {
        'LD_LIBRARY_PATH': '/diagnostic-opencl:' + inherited,
        'CLI_OpenCLFileName': '/usr/lib/x86_64-linux-gnu/libOpenCL.so.1',
        'CLI_LogToFile': '1', 'CLI_CallLogging': '1',
        'CLI_CallLoggingThreadId': '1', 'CLI_CallLoggingElapsedTime': '1',
        'CLI_CallLoggingEnqueueCounter': '1', 'CLI_FlushFiles': '1',
        'CLI_DumpDir': '/run/sentinel-delivery/ocl-api', 'CLI_AppendPid': '1',
    }
    if active_only:
        values.update(CLI_SentryActiveOnly='1', CLI_FlushFiles='0')
    (output / 'diagnostic-opencl.json').write_text(json.dumps({
        'sha256': hashlib.sha256(raw).hexdigest(), 'settings': values,
        'mode': 'active-call-shared-memory' if active_only else 'call-log',
        'scope': 'Official OpenCL ICD call logging; no kernel/queue/tensor overrides. '
                 'Instrumentation overhead means this is not a capacity result.',
    }, indent=2))
    arguments = ['-v', f'{directory.resolve()}:/diagnostic-opencl:ro']
    for key, value in values.items():
        arguments += ['-e', key + '=' + value]
    return arguments


def _run_probe(args: argparse.Namespace, owned: ProbeResources) -> tuple[dict, Path]:
    # Never SIGSTOP a native camera graph for cooling: its socket/model and
    # decoder deadlines keep advancing, which can manufacture reconnect faults.
    validation_phase('startup', native_clock_deadlines_active=True)
    gallery_limit = getattr(args, "capture_gallery_limit", 0)
    gallery_interval = getattr(args, "capture_gallery_interval_seconds", 0)
    memory_limit_mib = getattr(args, "memory_limit_mib", None)
    if memory_limit_mib is not None and (not isinstance(memory_limit_mib, int)
                                         or isinstance(memory_limit_mib, bool)
                                         or memory_limit_mib <= 0):
        raise ValueError('container memory limit must be a positive integer MiB value')
    if not math.isfinite(gallery_interval) or gallery_interval < 0:
        raise ValueError('capture gallery interval must be finite and nonnegative')
    engine = getattr(args, "engine", "podman")
    image_record = json.loads(subprocess.check_output(
        [engine, "image", "inspect", args.image], text=True))[0]
    image_id = image_record["Id"]
    if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", image_id):
        raise ValueError("image inspect did not return a full immutable configuration digest")
    if not AGENT.is_file() or not (SCHEMAS / "delivery-agent-package.schema.json").is_file():
        raise ValueError("tracked receiver and V8 schemas must exist before starting sources")
    run_id = getattr(args, "validation_run_id", None) or os.environ.get("SENTINEL_VALIDATION_RUN_ID")
    case_id = getattr(args, "validation_case", None) or os.environ.get("SENTINEL_VALIDATION_CASE")
    if bool(run_id) != bool(case_id):
        raise ValueError("validation run and case labels must both be supplied")
    startup_cpuset = getattr(args, "startup_cpuset", None)
    startup_affinity = getattr(args, "startup_affinity", None)
    startup_cpus = getattr(args, "startup_cpus", None)
    startup_period = getattr(args, 'startup_cpu_period_us', None)
    if startup_cpus is not None and not 0 < startup_cpus <= len(os.sched_getaffinity(0)):
        raise ValueError("startup CPU budget must fit available CPU capacity")
    if startup_period is not None and (startup_cpus is None or not 1000 <= startup_period <= 1000000
                                       or round(startup_cpus*startup_period) < 1000):
        raise ValueError('startup CPU period requires a valid budget, period and Linux quota')
    if startup_cpuset and startup_affinity:
        raise ValueError("choose one explicit startup affinity method")
    original_affinity = os.sched_getaffinity(0)
    if (startup_cpuset or startup_affinity) and not parse_cpu_set(
            startup_cpuset or startup_affinity).issubset(original_affinity):
        raise ValueError("startup CPU set must fit the controller's available CPUs")
    raw_recordings = getattr(args, "recordings_json", None) or os.environ.get("SENTRY_TEST_RECORDINGS")
    recordings = ([Path(p) for p in json.loads(raw_recordings)] if raw_recordings
                  else [Path(args.recording)] * args.cameras if args.recording else [])
    if recordings and (len(recordings) != args.cameras or any(not p.is_file() for p in recordings)):
        raise ValueError("one existing recording per camera is required")
    local_replay = bool(recordings)
    supplied_urls = json.loads(args.source_urls_json) if getattr(args, 'source_urls_json', None) else None
    if supplied_urls is not None and (local_replay or not isinstance(supplied_urls, list)
            or len(supplied_urls) != args.cameras or any(
                not isinstance(u, str) or urlsplit(u).scheme not in ('rtsp','rtsps','tcp','http','https')
                for u in supplied_urls)):
        raise ValueError('one valid network URL per camera, without local recordings, is required')
    output = Path(tempfile.mkdtemp(
        prefix=f"sentry-v8-capacity-{args.cameras}.", dir="/tmp"
    ))
    print(canonical({"artifact_directory": str(output), "image_config_id": image_id}), flush=True)
    warm_cache_seed = getattr(args, 'warm_cache_seed', None)
    cache_mounts = stage_compilation_cache(warm_cache_seed, image_id, output) if warm_cache_seed else []
    config_dir = output / "config"
    (output / "image-identity.json").write_text(json.dumps({
        "requested_image": args.image, "image_config_id": image_id,
        "repo_digests": image_record.get("RepoDigests", []),
        "engine": engine,
    }, indent=2) + "\n")
    harness_inputs = [Path(__file__), AGENT,
        ROOT / "scripts/isolated_validation/marker_sender.py",
        ROOT / "scripts/isolated_validation/reconnecting_replay.py",
        ROOT / "scripts/isolated_validation/mpegts_pacing.py",
        ROOT / "CAPACITY_TEST_MATRIX.json",
        ROOT / "SENTINEL_V6_PRODUCTION_POLICY.md", *SCHEMAS.glob("*.schema.json")]
    fingerprints = {str(p.relative_to(ROOT)): file_sha256(p)
                    for p in harness_inputs if p.is_file()}
    for source in harness_inputs:
        if source.is_file():
            target = output / "harness-snapshot" / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    recordings_inventory = {str(p): {"sha256": file_sha256(p), "size_bytes": p.stat().st_size}
                            for p in dict.fromkeys(recordings)}
    (output / "harness-provenance.json").write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": os.uname().nodename, "kernel": os.uname().release,
        "harness_schema_policy_hashes": fingerprints,
        "recordings": recordings_inventory,
        "runtime_overrides": [value.split("=", 1)[0] for value in args.container_env],
        "note": "Runtime override values are not printed: may contain secrets. Preserve nonsecret effective runtime configuration with the run.",
    }, indent=2) + "\n")
    secret_dir = output / "cameras"
    delivery_dir = output / "delivery"
    for path in (config_dir, secret_dir, delivery_dir):
        path.mkdir(mode=0o755)
    os.chmod(delivery_dir, 0o777)
    hook = getattr(args, 'diagnostic_hook', None)
    if hook:
        hook = Path(hook)
        if not hook.is_file():
            raise ValueError('diagnostic hook must be an existing test fixture')
        shutil.copyfile(hook, config_dir/'sitecustomize.py')
        (output/'diagnostic-hook.json').write_text(json.dumps({
            'sha256':file_sha256(hook), 'scope':'Injected test diagnostic; not an unmodified-image capacity result'},indent=2)+'\n')

    state = desired_state(
        args.cameras,
        args.fps,
        omit_fps=args.omit_fps,
        full_usecases=bool(args.full_usecases or getattr(args, "profile", None)
                           or getattr(args, "camera_profiles_json", None)),
    )
    per_camera_profiles = getattr(args, "camera_profiles_json", None)
    profiles = (json.loads(per_camera_profiles) if per_camera_profiles
                else [args.profile] * args.cameras if getattr(args, "profile", None) else None)
    if profiles is not None:
        apply_profiles(state, profiles)
    if getattr(args, "camera_fps_json", None):
        rates = json.loads(args.camera_fps_json)
        if len(rates) != args.cameras:
            raise ValueError("one requested FPS is required per camera")
        for camera, rate in zip(state["cameras"], rates):
            camera["fps"] = rate
    # Validate actual generated bindings/profile and rate before CV startup.
    from jsonschema import Draft202012Validator
    Draft202012Validator(json.loads((SCHEMAS / "desired-state.schema.json").read_text())).validate(state)
    if getattr(args, 'expected_outputs_json', None):
        validate_output_oracle(state, json.loads(args.expected_outputs_json))
    targets = {camera["camera_id"]: float(camera.get("fps", 8))
               for camera in state["cameras"]}

    source_processes: list[list[object | None]] = []
    source_log_dir = None

    def launch_source(index: int, *, append: bool = False) -> list[object]:
        port = args.source_port_base + index
        mode = "ab" if append else "wb"
        log_stream = (source_log_dir / f"source-{index + 1:02d}.log").open(mode)
        owned.handles.append(log_stream)
        command = ["python3", str(ROOT / "scripts/isolated_validation/reconnecting_replay.py"),
                   "--fixture", str(recordings[index]), "--port", str(port),
                   "--frame-log",str(source_log_dir / f"source-{index+1:02d}.frames.jsonl")]
        if getattr(args, "marker_replay", False):
            epochs = output / "sender-epochs"
            epochs.mkdir(exist_ok=True)
            command = ["python3", str(ROOT / "scripts/isolated_validation/marker_sender.py"),
                       "--port", str(port), "--fixture", str(recordings[index]),
                       "--logdir", str(epochs)]
        process = subprocess.Popen(command, stdout=log_stream, stderr=log_stream)
        owned.processes.append(process)
        return [process, log_stream]

    if local_replay:
        source_log_dir = output / "source-logs"
        source_log_dir.mkdir()
        source_urls = []
        for index in range(args.cameras):
            port = args.source_port_base + index
            source_urls.append(f"tcp://127.0.0.1:{port}")
            source_processes.append(launch_source(index))
        time.sleep(2)
        failed_sources = [
            index + 1 for index, (process, _) in enumerate(source_processes)
            if process.poll() is not None
        ]
        if failed_sources:
            raise RuntimeError(f"local TCP sources failed: {failed_sources}")
    elif supplied_urls is not None:
        source_urls = supplied_urls
    elif args.source_url:
        source_urls = [args.source_url for _ in range(args.cameras)]
    else:
        source_urls = [SOURCE_URLS[index % len(SOURCE_URLS)]
                       for index in range(args.cameras)]

    (config_dir / "desired_state.json").write_text(canonical(state) + "\n")
    (output / "desired-state-sha256.txt").write_text(file_sha256(config_dir / "desired_state.json") + "\n")
    for index, camera in enumerate(state["cameras"]):
        camera_id = camera["camera_id"]
        (secret_dir / f"{camera_id}.url").write_text(
            source_urls[index] + "\n"
        )
    os.chmod(config_dir / "desired_state.json", 0o644)
    for path in secret_dir.iterdir():
        os.chmod(path, 0o644)

    socket_path = delivery_dir / "agent.sock"
    agent_log = (output / "agent.log").open("wb")
    owned.handles.append(agent_log)
    agent_command = [
        "python3", str(AGENT), "--socket", str(socket_path),
        "--schemas", str(SCHEMAS), "--log", str(output / "packages.jsonl"),
        "--status", str(output / "agent-status.json"),
    ]
    if gallery_limit:
        agent_command.extend(["--gallery-dir", str(output / "gallery"),
                              "--gallery-limit", str(gallery_limit),
                              "--gallery-interval-seconds", str(gallery_interval)])
    agent = subprocess.Popen(agent_command, stdout=agent_log, stderr=subprocess.STDOUT)
    owned.processes.append(agent)
    deadline = time.monotonic() + 10
    while not socket_path.exists() and time.monotonic() < deadline:
        if agent.poll() is not None:
            raise RuntimeError("mock delivery agent exited during startup")
        time.sleep(0.1)
    if not socket_path.exists():
        raise RuntimeError("mock delivery-agent socket did not appear")

    name = f"sentinel-v8-capacity-{args.cameras}-{os.getpid()}"
    owned.cidfile = output / "container-id.txt"
    run = [
        engine, "run", "-d", "--name", name, "--network=host",
        "--cidfile", str(owned.cidfile),
        "--device", "/dev/dri",
        "-e", f"APEX_API_PORT={args.port}",
        "-e", "PYTHONFAULTHANDLER=1",
        "-v", f"{config_dir / 'desired_state.json'}:/configs/desired_state.json:ro",
        "-v", f"{secret_dir}:/run/secrets/sentinel/cameras:ro",
        "-v", f"{delivery_dir}:/run/sentinel-delivery",
    ]
    if engine == "podman":
        run.extend(["--group-add", "keep-groups"])
    else:
        for gid in sorted({p.stat().st_gid for base in ("/dev/dri", "/dev/accel")
                           if Path(base).exists() for p in Path(base).iterdir()}):
            run.extend(["--group-add", str(gid)])
    if run_id:
        run.extend(["--label", f"sentinel.validation_run={run_id}",
                    "--label", f"sentinel.validation_case={case_id}"])
    if startup_cpuset:
        run.extend(["--cpuset-cpus", startup_cpuset])
    if startup_cpus is not None:
        if startup_period is not None:
            run.extend(['--cpu-period', str(startup_period), '--cpu-quota',
                        str(round(startup_cpus*startup_period))])
        else:
            run.extend(["--cpus", str(startup_cpus)])
    if memory_limit_mib is not None:
        run.extend(["--memory", str(memory_limit_mib) + "m"])
    run.extend(cache_mounts)
    if args.gva_nireq is not None:
        run[6:6] = ["-e", f"SENTINEL_GVA_NIREQ={args.gva_nireq}"]
    for value in args.container_env:
        if "=" not in value or value.startswith("="):
            raise ValueError("--container-env requires KEY=VALUE")
        run.extend(["-e", value])
    if hook:
        if any(value.startswith('PYTHONPATH=') for value in args.container_env):
            raise ValueError('diagnostic hook preserves image PYTHONPATH; do not override it')
        inherited = next((e.split('=',1)[1] for e in image_record.get('Config',{}).get('Env',[])
                          if e.startswith('PYTHONPATH=')), '/opt/traffic-pilot:/opt/traffic-pilot/services/worker')
        run.extend(['-v',f'{config_dir}/sitecustomize.py:/diagnostic/sitecustomize.py:ro',
                    '-e','PYTHONPATH=/diagnostic:'+inherited])
    opencl_diagnostic = getattr(args, 'diagnostic_opencl_intercept', None)
    active_only = getattr(args, 'diagnostic_opencl_active_only', False)
    if active_only and not opencl_diagnostic:
        raise ValueError('active-call tracing requires an explicit diagnostic ICD')
    if opencl_diagnostic:
        if hook or any(value.startswith(('LD_LIBRARY_PATH=', 'CLI_'))
                       for value in args.container_env):
            raise ValueError('diagnostic ICD cannot be mixed with another hook/loader override')
        run.extend(stage_opencl_diagnostic(Path(opencl_diagnostic), image_record, output, active_only))
    crash_tracer = getattr(args, 'diagnostic_crash_tracer', None)
    if crash_tracer:
        if hook or opencl_diagnostic or any(v.startswith('LD_PRELOAD=') for v in args.container_env):
            raise ValueError('fault tracer cannot be mixed with another diagnostic loader/hook')
        path = Path(crash_tracer).resolve()
        if not path.is_file():
            raise ValueError('native fault tracer fixture missing')
        copied = config_dir/'native-crash-trace.so'
        shutil.copyfile(path,copied)
        run.extend(['-v',str(copied)+':/sentry-test-native-crash-trace.so:ro',
                    '-e','LD_PRELOAD=/sentry-test-native-crash-trace.so'])
        (output/'diagnostic-crash-tracer.json').write_text(json.dumps({
            'sha256':file_sha256(copied), 'scope':'Test-only native fatal-signal trace; not capacity evidence'},indent=2)+'\n')
    input_capture = getattr(args, 'diagnostic_fire_input_capture', None)
    if input_capture:
        if crash_tracer or opencl_diagnostic or any(v.startswith('LD_PRELOAD=') for v in args.container_env):
            raise ValueError('input capture cannot be mixed with another native loader override')
        path = Path(input_capture).resolve()
        if not path.is_file():
            raise ValueError('fire input capture fixture missing')
        copied = config_dir/'fire-input-capture.so'
        shutil.copyfile(path, copied)
        run.extend(['-v', str(copied)+':/sentry-test-fire-input-capture.so:ro',
                    '-e', 'LD_PRELOAD=/sentry-test-fire-input-capture.so',
                    '-e', 'SENTINEL_FIRE_INPUT_CAPTURE=/run/sentinel-delivery/fire-model-inputs'])
        (output/'diagnostic-fire-input-capture.json').write_text(json.dumps({
            'sha256':file_sha256(copied),
            'scope':'Read-only bounded OpenVINO input capture; instrumentation changes timing, not capacity evidence'
        },indent=2)+'\n')
    if Path("/dev/accel/accel0").exists():
        run[6:6] = ["--device", "/dev/accel/accel0"]
    run.append(image_id)
    if startup_affinity:
        run = ["taskset", "-c", startup_affinity, *run]

    samples: list[dict] = []
    isolation_samples: list[dict] = []
    outage_started_at = None
    outage_restored_at = None
    outage_index = (args.outage_camera - 1) if args.outage_camera else None
    if outage_index is not None and not local_replay:
        raise ValueError("--outage-camera requires --recording")
    if outage_index is not None and not 0 <= outage_index < args.cameras:
        raise ValueError("--outage-camera must identify a configured camera")
    logs = ""
    last_health_payload: dict | None = None
    final_health_available = False
    steady_started_at = None
    steady_ended_at = None
    started = time.monotonic()
    startup_completed = None
    try:
        launched = subprocess.run(run, check=True, timeout=60, capture_output=True, text=True)
        container_id = launched.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise RuntimeError("container engine did not return an owned container ID")
        owned.container_id = container_id
        (output / "container-id.txt").write_text(container_id + "\n")
        print(f"[{args.cameras}] container started; waiting for all cameras", flush=True)
        deadline = startup_monotonic() + args.startup_timeout
        while startup_monotonic() < deadline:
            payload = health(args.port)
            if payload:
                last_health_payload = payload
                summary = summarize(payload, args.cameras, args.fps, targets)
                if (summary["camera_records"] == args.cameras
                        and summary["cameras_with_recent_frames"] == args.cameras):
                    startup_completed = time.monotonic()
                    samples.append(summary)
                    print(
                        f"[{args.cameras}] all cameras active after "
                        f"{startup_completed - started:.1f}s; "
                        f"decode={summary['aggregate_decode_fps']}/"
                        f"{summary['target_decode_fps']} fps",
                        flush=True,
                    )
                    break
                if int(time.monotonic() - started) % 30 == 0:
                    print(
                        f"[{args.cameras}] startup: records={summary['camera_records']} "
                        f"recent={summary['cameras_with_recent_frames']} "
                        f"decode={summary['aggregate_decode_fps']}",
                        flush=True,
                    )
            time.sleep(1)

        if startup_completed is None:
            print(f"[{args.cameras}] startup timeout", flush=True)
        else:
            validation_phase('warmup', native_clock_deadlines_active=True)
            if startup_affinity:
                restore = '''import os,json,pathlib,sys
requested=set(json.loads(sys.argv[1]));changed=[]
for process in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  uid=next(l.split()[1] for l in (process/'status').read_text().splitlines() if l.startswith('Uid:'))
  if int(uid)!=os.getuid():continue
  for task in (process/'task').iterdir():
   try:
    tid=int(task.name);os.sched_setaffinity(tid,requested)
    actual=os.sched_getaffinity(tid)
    if actual!=requested:raise RuntimeError('thread affinity not restored')
    changed.append(tid)
   except ProcessLookupError:pass
 except (FileNotFoundError,ProcessLookupError):pass
if not changed:raise RuntimeError('no owned CV threads verified')
print(json.dumps({'requested':sorted(requested),'verified_thread_ids':changed}))'''
                result = subprocess.run([engine, "exec", "--user", "10001", container_id,
                                         "python3", "-c", restore, json.dumps(sorted(original_affinity))],
                                        check=True, capture_output=True, text=True, timeout=15)
                proof = json.loads(result.stdout)
                proof.update(startup_affinity=startup_affinity, released_monotonic=time.monotonic(),
                             scope='Test-only inherited startup affinity; all owned CV threads restored before timing')
                (output / 'startup-affinity-release.json').write_text(json.dumps(proof, indent=2) + '\n')
            if startup_cpuset:
                restored = ','.join(str(c) for c in sorted(original_affinity))
                subprocess.run([engine, "update", "--cpuset-cpus", restored, container_id],
                               check=True, capture_output=True, text=True, timeout=15)
                inspected = json.loads(subprocess.check_output([engine, "inspect", container_id], text=True))[0]
                pid = inspected['State']['Pid']
                actual = os.sched_getaffinity(pid)
                relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1]
                effective_path = Path('/sys/fs/cgroup') / relative.lstrip('/') / 'cpuset.cpus.effective'
                effective = parse_cpu_set(effective_path.read_text().strip())
                (output / 'startup-affinity-release.json').write_text(json.dumps({
                    'startup_cpuset': startup_cpuset, 'steady_affinity_requested': sorted(original_affinity),
                    'actual_init_affinity': sorted(actual), 'cgroup_effective_cpus': sorted(effective),
                    'released_monotonic': time.monotonic(),
                    'scope': 'Test-only startup condition; no host power/fan or image gate changes.'}, indent=2) + '\n')
                if actual != original_affinity or effective != original_affinity:
                    raise RuntimeError('full steady CPU affinity not restored; measurement is invalid')
            if startup_cpus is not None:
                subprocess.run(startup_quota_release_command(
                    engine, container_id, len(original_affinity), startup_period),
                               check=True, capture_output=True, text=True, timeout=15)
                inspected = json.loads(subprocess.check_output([engine, "inspect", container_id], text=True))[0]
                relative = Path(f'/proc/{inspected["State"]["Pid"]}/cgroup').read_text().strip().split('::', 1)[1]
                maximum = (Path('/sys/fs/cgroup') / relative.lstrip('/') / 'cpu.max').read_text().strip()
                quota, period = maximum.split()
                if quota != 'max' and int(quota) / int(period) < len(original_affinity):
                    raise RuntimeError('startup CPU quota not restored before measurement')
                (output / 'startup-quota-release.json').write_text(json.dumps({
                    'startup_cpus': startup_cpus, 'steady_capacity_cores': len(original_affinity),
                    'verified_cpu_max': maximum, 'released_monotonic': time.monotonic(),
                    'scope': 'Conditional test-only cold startup CPU quota, removed before steady timing'}, indent=2) + '\n')
            if args.warmup_seconds > 0:
                print(
                    f"[{args.cameras}] warming shared graph for "
                    f"{args.warmup_seconds}s",
                    flush=True,
                )
                warm_until = startup_monotonic() + args.warmup_seconds
                while startup_monotonic() < warm_until:
                    time.sleep(min(.5, max(.01, warm_until - startup_monotonic())))
            validation_phase('steady', native_clock_deadlines_active=True)
            steady_started_at = time.monotonic()
            steady_deadline = steady_started_at + args.steady_seconds
            next_sample = time.monotonic()
            while time.monotonic() < steady_deadline:
                now = time.monotonic()
                if (outage_index is not None and outage_started_at is None
                        and now >= startup_completed + args.outage_after_seconds):
                    process, log_stream = source_processes[outage_index]
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    log_stream.close()
                    source_processes[outage_index] = [None, None]
                    outage_started_at = time.monotonic()
                    print(
                        f"[{args.cameras}] stopped source for camera "
                        f"{outage_index + 1}; checking peer isolation",
                        flush=True,
                    )
                if (outage_started_at is not None and outage_restored_at is None
                        and now >= outage_started_at + args.outage_seconds):
                    source_processes[outage_index] = launch_source(
                        outage_index, append=True
                    )
                    outage_restored_at = time.monotonic()
                    print(
                        f"[{args.cameras}] restored source for camera "
                        f"{outage_index + 1}; waiting for branch reconnect",
                        flush=True,
                    )
                if time.monotonic() >= next_sample:
                    payload = health(args.port)
                    if payload:
                        last_health_payload = payload
                        summary = summarize(payload, args.cameras, args.fps, targets)
                        summary["sample_phase"] = "steady"
                        summary["sample_monotonic_seconds"] = time.monotonic()
                        samples.append(summary)
                        if outage_started_at is not None:
                            isolation_samples.append({
                                "sample_monotonic_seconds": time.monotonic(),
                                "ready": bool(payload.get('ready')),
                                "phase": (
                                    "outage" if outage_restored_at is None
                                    else "recovery"
                                ),
                                "seconds_since_outage": round(
                                    time.monotonic() - outage_started_at, 3
                                ),
                                "cameras": camera_runtime_snapshot(payload),
                            })
                        print(
                            f"[{args.cameras}] steady: decode="
                            f"{summary['aggregate_decode_fps']}/"
                            f"{summary['target_decode_fps']} "
                            f"ratio={summary['decode_target_ratio']:.3f} "
                            f"recent={summary['cameras_with_recent_frames']} "
                            f"mem={summary['container_memory_mb']}MB "
                            f"gpu={summary['gpu_utilization_pct']}%",
                            flush=True,
                        )
                    else:
                        samples.append({"sample_phase": "steady", "sample_monotonic_seconds": time.monotonic(),
                                        "health_unavailable": True, "per_camera": {},
                                        "target_decode_fps": sum(targets.values()),
                                        "decode_target_ratio": 0.0})
                    next_sample += args.sample_seconds
                time.sleep(0.5)
    finally:
        validation_phase('cleanup')
        steady_ended_at = time.monotonic()
        final_health = health(args.port)
        if final_health:
            final_health_available = True
            last_health_payload = final_health
        if last_health_payload:
            (output / "healthz-final.json").write_text(
                json.dumps(last_health_payload, indent=2) + "\n"
            )
        try:
            if not owned.container_id:
                raise OSError("No owned container ID; not collecting another container's logs")
            result = subprocess.run(
                [engine, "logs", owned.container_id], text=True, capture_output=True, timeout=20
            )
            logs = result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            logs = "test collector log failure: " + type(exc).__name__
        (output / "container.log").write_text(logs)
        owned.close()
        (output / "cleanup.json").write_text(json.dumps({"errors": owned.errors}, indent=2) + "\n")

    fatal_pattern = re.compile(
        r"segmentation fault|sigsegv|sigabrt|core dumped|"
        r"multi_graphics_allocation|camera process (?:group )?exited|"
        r"longjmp causes uninitialized stack frame|CL_OUT_OF_RESOURCES|"
        r"camera_thread_unhandled_exception|secondary produced no output tensor|"
        r'dls_secondary_job.*"successful"\s*:\s*false',
        re.IGNORECASE,
    )
    fatal_lines = [line for line in logs.splitlines() if fatal_pattern.search(line)]
    # During an intentional source outage, the rolling FPS window necessarily
    # includes the missing interval after the branch has reconnected.  Require
    # two consecutive final healthy windows for that fault-injection case;
    # ordinary capacity qualification retains the stricter three-window rule.
    required_stable_samples = 2 if outage_index is not None else 3
    stable_samples = samples[-min(required_stable_samples, len(samples)):]
    minimum_ratio = min(
        (float(item["decode_target_ratio"]) for item in stable_samples),
        default=0.0,
    )
    final = samples[-1] if samples else (
        summarize(last_health_payload, args.cameras, args.fps, targets)
        if last_health_payload else {}
    )
    handoff = final.get("handoff") or {}
    end_health = summarize(last_health_payload, args.cameras, args.fps, targets) if last_health_payload else {}
    package_summary = {
        "package_count": 0,
        "event_types": {},
        "evidence_objects": 0,
        "embedding_objects": 0,
        "embedding_profiles": {},
    }
    packages_path = output / "packages.jsonl"
    packages = []
    if packages_path.is_file():
        for line in packages_path.read_text().splitlines():
            if not line.strip():
                continue
            package = json.loads(line)
            packages.append(package)
            package_summary["package_count"] += 1
            event_type = str(package.get("event_type") or "missing")
            package_summary["event_types"][event_type] = (
                package_summary["event_types"].get(event_type, 0) + 1
            )
            evidence = package.get("evidence") or []
            embeddings = package.get("embeddings") or []
            package_summary["evidence_objects"] += len(evidence)
            package_summary["embedding_objects"] += len(embeddings)
            for profile in embeddings:
                profile = str(profile)
                package_summary["embedding_profiles"][profile] = (
                    package_summary["embedding_profiles"].get(profile, 0) + 1
                )
    full_usecases_passed = True
    if args.full_usecases:
        full_usecases_passed = bool(
            package_summary["event_types"].get("camera_health")
            and package_summary["event_types"].get("zone_count")
            and package_summary["event_types"].get("scene_sample")
            and package_summary["evidence_objects"] > 0
            and package_summary["embedding_profiles"].get("siglip2-base-v1")
        )
    source_outages = ({f'capacity-{outage_index+1:02d}': [(
        outage_started_at, outage_restored_at or steady_ended_at)]}
        if outage_index is not None and outage_started_at is not None else {})
    output_coverage = steady_package_coverage(
        packages, state, steady_started_at or steady_ended_at, steady_ended_at,
        activity=getattr(args, "activity", "unspecified"),
        expected=json.loads(args.expected_outputs_json) if getattr(args, "expected_outputs_json", None) else None,
        unavailable_intervals=source_outages,
    )
    utc_sanity = source_utc_sanity(packages)
    isolation_passed = True
    if outage_index is not None:
        target_id = f"capacity-{outage_index + 1:02d}"
        interruption_records = [
            item for item in isolation_samples
            if (
                (item["cameras"].get(target_id) or {}).get("state")
                in {"degraded", "unavailable"}
                or int((item["cameras"].get(target_id) or {}).get(
                    "decoder_errors"
                ) or 0) > 0
                or int((item["cameras"].get(target_id) or {}).get(
                    "reconnects"
                ) or 0) > 0
            )
        ]
        recovery_records = [
            item for item in isolation_samples if item["phase"] == "recovery"
        ]
        target_interrupted = bool(interruption_records)
        peers_healthy = isolation_peers_progress(isolation_samples, target_id, targets)
        last_target_before_restore = max((int((item['cameras'].get(target_id) or {}).get('decoded_frames',0))
            for item in isolation_samples if item['phase']=='outage'), default=0)
        target_recovered = any(
            (item["cameras"].get(target_id) or {}).get("state") == "healthy"
            and 0 <= float((item['cameras'].get(target_id) or {}).get('last_frame_age_seconds', float('inf'))) < 4
            and int((item['cameras'].get(target_id) or {}).get('decoded_frames',0)) > last_target_before_restore+5
            and int((item["cameras"].get(target_id) or {}).get("reconnects") or 0) > 0
            for item in recovery_records
        )
        isolation_passed = bool(
            target_interrupted and peers_healthy and target_recovered
        )
    else:
        target_interrupted = peers_healthy = target_recovered = None
    window_minimum = getattr(args, "minimum_window_ratio", .90)
    measured_throughput = steady_throughput(samples, args.minimum_decode_ratio, window_minimum)
    completed_work = steady_completed_work(samples, args.minimum_decode_ratio, window_minimum,
                                           getattr(args, "activity", "unspecified"))
    passed = bool(
        startup_completed is not None
        and len(stable_samples) >= required_stable_samples
        # Rolling health FPS describes its own (often 2s) telemetry window.
        # The declared acceptance window is the measured interval between
        # cumulative counter samples, not a single rolling snapshot.
        and (outage_index is None or steady_throughput(
            stable_samples, window_minimum, window_minimum)["passed"])
        and (outage_index is not None or measured_throughput["passed"])
        and (outage_index is not None or completed_work["passed"])
        and final.get("cameras_with_recent_frames") == args.cameras
        and final.get("camera_states", {}).get("healthy") == args.cameras
        and end_health.get("cameras_with_recent_frames") == args.cameras
        and end_health.get("camera_states", {}).get("healthy") == args.cameras
        and int(handoff.get("protocol_errors") or 0) == 0
        and int(handoff.get("exhausted") or 0) == 0
        and isolation_passed
        and full_usecases_passed
        and not owned.errors
        and final_health_available
        and output_coverage["passed"]
        and utc_sanity["passed"]
        and not fatal_lines
    )
    report = {
        "image": args.image,
        "image_config_id": image_id,
        "engine": engine,
        "validation_run_id": run_id,
        "validation_case": case_id,
        "target_fps_by_camera": targets,
        "camera_count": args.cameras,
        "fps_per_camera": next(iter(set(targets.values()))) if len(set(targets.values())) == 1 else None,
        "requested_default_fps": args.fps,
        "fps_explicit_in_desired_state": not args.omit_fps,
        "source_urls": [sanitized_url(value) for value in source_urls],
        "source_reuse": source_urls_reused(source_urls),
        "source_reuse_scope": "Exact URI reuse only; distinct URLs do not prove different physical scenes or fixtures",
        "source_mode": (
            "local_independent_tcp_replays" if local_replay
            else "explicit_live_source" if args.source_url
            else "explicit_camera_urls" if args.source_urls_json
            else "live_tcp"
        ),
        "motion_gate_enabled": True,
        "full_usecases": args.full_usecases,
        "camera_workload_profiles": profiles,
        "diagnostic_hook_injected": bool(hook),
        "diagnostic_opencl_intercept_injected": bool(opencl_diagnostic),
        "diagnostic_crash_tracer_injected": bool(crash_tracer),
        "diagnostic_fire_input_capture_injected": bool(input_capture),
        "recordings": [str(p) for p in recordings],
        "startup_cpuset": startup_cpuset,
        "startup_affinity": startup_affinity,
        "startup_cpus": startup_cpus,
        "startup_cpu_period_us": startup_period,
        "container_memory_limit_mib": memory_limit_mib,
        "compilation_cache_condition": 'warm-same-image-host-seed' if warm_cache_seed else 'fresh-container-default',
        "declared_startup_cooling_seconds": startup_pause_seconds(),
        "steady_throughput": measured_throughput,
        "steady_completed_work": completed_work,
        "throughput_smoke_passed": passed,
        "capacity_qualified": False,
        "capacity_qualified_scope": "No full qualification from this probe alone. Join same-run independent freshness, resources, positive-workload and sustained evidence before claiming a capacity point.",
        "steady_output_coverage": output_coverage,
        "scheduled_source_outages_monotonic": source_outages,
        "source_utc_sanity": utc_sanity,
        "steady_started_monotonic": steady_started_at,
        "steady_ended_monotonic": steady_ended_at,
        "test_scope": ("diagnostic" if hook or opencl_diagnostic or crash_tracer or input_capture else
                       "isolation" if outage_index is not None else "capacity"),
        "end_health": end_health,
        "final_health_available": final_health_available,
        "startup_seconds": (
            round(startup_completed - started, 3) if startup_completed else None
        ),
        "steady_seconds": args.steady_seconds,
        "minimum_required_decode_ratio": args.minimum_decode_ratio,
        "required_final_stable_samples": required_stable_samples,
        "minimum_observed_decode_ratio": round(minimum_ratio, 4),
        "rolling_snapshot_ratio_is_diagnostic_only": True,
        "passed": passed,
        "fatal_lines": fatal_lines,
        "final": final,
        "samples": samples,
        "isolation": {
            "requested": outage_index is not None,
            "camera_number": args.outage_camera,
            "target_interrupted": target_interrupted,
            "peers_healthy": peers_healthy,
            "target_recovered": target_recovered,
            "passed": isolation_passed,
            "samples": isolation_samples,
        },
        "package_summary": package_summary,
        "full_usecases_minimum_outputs_passed": full_usecases_passed,
        "artifact_directory": str(output),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"[{args.cameras}] {'PASS' if passed else 'FAIL'} artifacts={output}", flush=True)
    print(canonical(report), flush=True)
    return report, output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--engine", choices=["podman", "docker"], default="podman")
    parser.add_argument("--validation-run-id")
    parser.add_argument("--validation-case")
    parser.add_argument("--cameras", type=int, required=True)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument(
        "--omit-fps", action="store_true",
        help="omit camera FPS and verify the contract/runtime default of 8",
    )
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--startup-timeout", type=int, default=240)
    parser.add_argument("--steady-seconds", type=int, default=90)
    parser.add_argument("--warmup-seconds", type=int, default=0)
    parser.add_argument("--memory-limit-mib", type=int,
                        help="Explicit test container RAM envelope; protects host headroom and is recorded, not an image gate")
    parser.add_argument("--sample-seconds", type=int, default=15)
    parser.add_argument("--minimum-decode-ratio", type=float, default=0.95)
    parser.add_argument("--minimum-window-ratio", type=float, default=0.90)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--marker-replay", action="store_true",
                        help="Use tracked TCP/PES marker sender with an already marked fixture; never edits runtime or source pixels")
    parser.add_argument("--recordings-json", help="JSON list of exactly one local replay per camera")
    parser.add_argument("--profile", help="Uniform capacity descriptor ID, e.g. S, SIFBG, SVX or SA")
    parser.add_argument("--camera-profiles-json", help="JSON list of one descriptor ID per camera")
    parser.add_argument("--camera-fps-json", help="JSON list of one requested FPS per camera")
    parser.add_argument("--activity", choices=["active", "quiet", "mixed", "unspecified"], default="unspecified")
    parser.add_argument("--expected-outputs-json", help="Independent positive fixture oracle: per-camera events and embedding_profiles required in steady window")
    parser.add_argument("--capture-gallery-limit", type=int, default=0,
                        help="Bounded package gallery for pixel/provenance checks; fixture-only")
    parser.add_argument("--capture-gallery-interval-seconds", type=float, default=0,
                        help="Retain JPEGs throughout a soak, per camera/session/binding/modality; audit every package")
    parser.add_argument('--diagnostic-fire-input-capture', type=Path,
                        help='Test-only bounded OpenVINO fire input observer; never capacity qualification')
    parser.add_argument('--diagnostic-hook', type=Path,
                        help='Explicit sitecustomize test hook, hashed and marked as injected diagnostic work')
    parser.add_argument('--diagnostic-opencl-intercept', type=Path,
                        help='Explicit official ICD tracing library, frozen and marked diagnostic; no image mutation')
    parser.add_argument('--diagnostic-opencl-active-only', action='store_true',
                        help='Diagnostic patched ICD shared-memory progress instead of per-call logging')
    parser.add_argument('--diagnostic-crash-tracer', type=Path,
                        help='Test-only fatal native stack tracer preload; explicitly not an unmodified capacity run')
    parser.add_argument("--startup-cpuset", help="Explicit test-only startup affinity, restored and verified before steady measurement")
    parser.add_argument("--startup-affinity", help="Test-only inherited startup affinity for engines without cpuset delegation; all owned CV threads restored before timing")
    parser.add_argument("--startup-cpus", type=float, help="Test-only cold startup CPU quota; restored to full host capacity and verified before steady timing")
    parser.add_argument('--startup-cpu-period-us', type=int,
                        help='Explicit Linux startup quota period, removed as a limiting budget before warmup; no native SIGSTOP cooling')
    parser.add_argument('--warm-cache-seed', type=Path,
                        help='Frozen same-image/host compiler-cache fixture; labelled warm start, never restores camera state')
    parser.add_argument(
        "--container-env", action="append", default=[], metavar="KEY=VALUE",
        help="bounded runtime override used by qualification experiments",
    )
    parser.add_argument("--source-url")
    parser.add_argument("--source-urls-json", help="Exact ordered URL list for independent live cameras; URLs stored only in private Secret fixtures")
    parser.add_argument("--full-usecases", action="store_true")
    parser.add_argument("--source-port-base", type=int, default=19001)
    parser.add_argument("--outage-camera", type=int)
    parser.add_argument("--outage-after-seconds", type=int, default=20)
    parser.add_argument("--outage-seconds", type=int, default=20)
    parser.add_argument("--gva-nireq", type=int, choices=range(1, 9))
    args = parser.parse_args()
    if not 1 <= args.cameras <= 50:
        parser.error("--cameras must be between 1 and 50")
    if args.recording and not args.recording.is_file():
        parser.error("--recording must be an existing video file")
    report, _ = run_probe(args)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
