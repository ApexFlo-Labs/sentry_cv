"""No stress commands, real process signals, or container operations are used."""

from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "guarded_case_under_test", Path(__file__).with_name("run_guarded_case.py"))
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


class Sensors:
    def __init__(self, *samples):
        self.samples = list(samples)
        self.index = 0

    def __call__(self):
        value = self.samples[min(self.index, len(self.samples) - 1)]
        self.index += 1
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, list) else [guard.Reading("cpu", value, 100)]


class Child:
    def __init__(self, exit_code=0, polls=0):
        self.pid, self.returncode = 4321, None
        self.exit_code, self.polls = exit_code, polls

    def poll(self):
        if self.polls <= 0:
            self.returncode = self.exit_code
        self.polls -= 1
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -signal.SIGTERM
        return self.returncode


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = guard.Config(Path(self.temp.name), "sample", "podman", "run-1",
                                   ("probe", "--source-url", "rtsp://u:pass@camera:8554/path?token=xyz"),
                                   cool_dwell=2, sample_interval=1, max_cooldown=10)
        self.clock = Clock()
        self.spawns, self.engine_calls, self.signals = [], [], []
        self.children = [Child()]

    def spawn(self, command, **kwargs):
        self.spawns.append((command, kwargs, self.clock()))
        kwargs["stdout"].write("private diagnostic: token=raw-secret\n")
        return self.children.pop(0)

    def engine(self, argv, **kwargs):
        self.engine_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    def supervisor(self, sensors=None, pressure=None, **changes):
        return guard.Supervisor(
            replace(self.config, **changes), sensor_reader=sensors or Sensors(60),
            pressure_reader=pressure or (lambda root: {
                "available_ram_bytes": 8 * 1024 ** 3, "free_disk_bytes": 8 * 1024 ** 3}),
            popen=self.spawn, engine_runner=self.engine,
            killpg=lambda pid, sig: self.signals.append((pid, sig)),
            monotonic=self.clock, wait=self.clock.wait)

    def records(self, supervisor):
        return [json.loads(line) for line in
                (supervisor.directory / "events.jsonl").read_text().splitlines()]

    def status(self, supervisor):
        return json.loads((supervisor.directory / "status.json").read_text())

    def test_import_is_inert(self):
        with patch.object(subprocess, "Popen", side_effect=AssertionError("spawn on import")), \
                patch.object(subprocess, "run", side_effect=AssertionError("engine on import")):
            SPEC.loader.exec_module(guard)

    def test_stopped_log_capture_keeps_stderr_without_corrupting_inspect_json(self):
        supervisor = self.supervisor()
        supervisor.engine_runner = lambda *a, **k: SimpleNamespace(
            returncode=0, stdout='{"ok":true}', stderr='worker diagnostic on stderr\n')
        self.assertEqual(supervisor.engine('inspect'), '{"ok":true}')
        self.assertIn('worker diagnostic on stderr', supervisor.engine(
            'logs', 'owned-id', include_stderr=True))

    def test_near_critical_temperature_does_not_wait_for_hot_vote_count(self):
        self.children = [Child(polls=100)]
        supervisor = self.supervisor(sensors=Sensors(60, 60, 60, 99),
                                     max_attempts=1, hot_samples=100)
        self.assertEqual(supervisor.run(), 2)
        self.assertTrue(any(r['event'] == 'near_critical_temperature' for r in self.records(supervisor)))
        self.assertEqual(self.status(supervisor)['status'], 'BLOCKED_THERMAL')

    def test_cold_dwell_environment_private_logs_and_metadata(self):
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.spawns[0][2], 2)
        command, options, _ = self.spawns[0]
        self.assertEqual(command, self.config.command)  # child receives original URL
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["env"]["SENTINEL_VALIDATION_RUN_ID"], "run-1")
        self.assertEqual(options["env"]["SENTINEL_VALIDATION_CASE"], "sample")
        self.assertEqual(self.status(supervisor)["status"], "PASS")
        self.assertFalse(self.engine_calls)
        for record in self.records(supervisor):
            for key in ("utc", "monotonic", "run_id", "command", "attempt", "temperatures"):
                self.assertIn(key, record)
            self.assertNotIn("token=xyz", json.dumps(record))
            self.assertNotIn("u:pass", json.dumps(record))
        for path in supervisor.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            if path.suffix != ".log":
                self.assertNotIn("raw-secret", path.read_text())
        self.assertIn("raw-secret", (supervisor.directory / "attempt-1.stdout.log").read_text())
        self.assertEqual(stat.S_IMODE(supervisor.directory.stat().st_mode), 0o700)

    def test_hot_prestart_and_dwell_reset_hysteresis(self):
        supervisor = self.supervisor(Sensors(92, 69, 71, 70, 69, 68, 65))
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.spawns[0][2], 5)
        self.assertFalse(self.engine_calls)

    def test_critical_margin_tightens_resume_and_abort(self):
        readings = Sensors([guard.Reading("cpu", 66, 75)],
                           [guard.Reading("cpu", 64, 75)])
        supervisor = self.supervisor(readings)
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.spawns[0][2], 3)
        self.assertEqual(self.status(supervisor)["temperatures"][0]["abort_ceiling"], 65)

    def test_hot_samples_abort_then_cool_retry_same_case(self):
        self.children = [Child(polls=100), Child()]
        supervisor = self.supervisor(Sensors(60, 60, 60, 90, 91, 92, 80, 69, 69, 69, 60))
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(self.spawns), 2)
        self.assertEqual(self.spawns[0][0], self.spawns[1][0])
        self.assertEqual(self.spawns[1][2], 7)
        attempts = self.status(supervisor)["attempts"]
        self.assertEqual([a["result"] for a in attempts], ["EXCLUDED_RESOURCE_ABORT", "PASS"])
        self.assertEqual(attempts[0]["reason"], "thermal")
        self.assertEqual(attempts[0]["child_exit_code"], -signal.SIGTERM)
        for attempt in (1, 2):
            for suffix in ("stdout.log", "stderr.log", "result.json"):
                self.assertTrue((supervisor.directory / f"attempt-{attempt}.{suffix}").exists())
        self.assertTrue(all(pid == 4321 for pid, _ in self.signals))

    def test_single_hot_spikes_do_not_accumulate(self):
        self.children = [Child(polls=5)]
        supervisor = self.supervisor(Sensors(60, 60, 60, 90, 60, 90, 60, 90, 60))
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(self.spawns), 1)
        self.assertFalse(self.engine_calls)

    def test_exhausted_thermal_is_blocked_exit_two_not_fail(self):
        self.children = [Child(polls=100) for _ in range(3)]
        supervisor = self.supervisor(Sensors(60, 91, 60, 91, 60, 91),
                                     cool_dwell=0, hot_samples=1)
        self.assertEqual(supervisor.run(), 2)
        status = self.status(supervisor)
        self.assertEqual(status["status"], "BLOCKED_THERMAL")
        self.assertEqual(len(status["attempts"]), 3)
        self.assertTrue(all(a["result"] == "EXCLUDED_RESOURCE_ABORT" for a in status["attempts"]))

    def test_maximum_cooldown_blocks_without_start(self):
        supervisor = self.supervisor(Sensors(80))
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_THERMAL")
        self.assertEqual(self.clock(), 10)
        self.assertFalse(self.spawns)

    def test_missing_untrustworthy_and_unreadable_sensors_block(self):
        for sample in ([], [guard.Reading("cpu", float("nan"))],
                       [guard.Reading("cpu", 0)], [guard.Reading("cpu", 60, -1)],
                       OSError("unreadable")):
            with self.subTest(sample=sample):
                supervisor = self.supervisor(Sensors(sample), run_id="sensor-" + str(len(list(Path(self.temp.name).iterdir()))))
                self.assertEqual(supervisor.run(), 2)
                self.assertEqual(self.status(supervisor)["status"], "BLOCKED_SENSOR")
        self.assertFalse(self.spawns)

    def test_sensor_disappearing_during_run_aborts_without_retry(self):
        self.children = [Child(polls=100)]
        both = [guard.Reading("cpu", 60), guard.Reading("zone", 60)]
        supervisor = self.supervisor(Sensors(both, both, both, [guard.Reading("cpu", 60)]))
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_SENSOR")
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.signals)

    def test_ram_and_disk_pressure_are_separate_from_thermal(self):
        for key in ("available_ram_bytes", "free_disk_bytes"):
            def pressure(root):
                result = {"available_ram_bytes": 8 * 1024 ** 3, "free_disk_bytes": 8 * 1024 ** 3}
                result[key] = 1
                return result
            supervisor = self.supervisor(pressure=pressure, run_id=key)
            self.assertEqual(supervisor.run(), 2)
            self.assertEqual(self.status(supervisor)["status"], "BLOCKED_RESOURCE_PRESSURE")
        self.assertFalse(self.spawns)

    def test_pressure_during_run_aborts_and_does_not_retry(self):
        self.children = [Child(polls=100)]
        calls = []
        def pressure(root):
            calls.append(root)
            return {"available_ram_bytes": 1 if len(calls) > 3 else 8 * 1024 ** 3,
                    "free_disk_bytes": 8 * 1024 ** 3}
        supervisor = self.supervisor(pressure=pressure)
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_RESOURCE_PRESSURE")
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.engine_calls)

    def test_pressure_unavailable_blocks(self):
        supervisor = self.supervisor(pressure=lambda root: {})
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_PRESSURE_UNAVAILABLE")

    def test_nonthermal_failure_does_not_retry(self):
        self.children = [Child(exit_code=7)]
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 1)
        self.assertEqual(self.status(supervisor)["status"], "FAIL")
        self.assertEqual(self.status(supervisor)["attempts"][0]["child_exit_code"], 7)
        self.assertEqual(len(self.spawns), 1)

    def test_container_stops_require_both_exact_labels(self):
        ids = [str(i) * 12 for i in range(1, 5)]
        labels = [{"sentinel.validation_run": "run-1", "sentinel.validation_case": "sample"},
                  {"sentinel.validation_run": "run-1"},
                  {"sentinel.validation_run": "other", "sentinel.validation_case": "sample"},
                  {"sentinel.validation_run": "run-1", "sentinel.validation_case": "other"}]
        supervisor = self.supervisor()
        def engine(argv, **kwargs):
            self.engine_calls.append(argv)
            output = "\n".join(ids) if argv[1] == "ps" else (
                json.dumps(labels[ids.index(argv[-1])]) if argv[1] == "inspect" else "")
            return SimpleNamespace(returncode=0, stdout=output)
        supervisor.engine_runner = engine
        supervisor.emit = lambda *args, **kwargs: None
        supervisor.stop_containers()
        self.assertIn("label=sentinel.validation_run=run-1", self.engine_calls[0])
        self.assertIn("label=sentinel.validation_case=sample", self.engine_calls[0])
        self.assertEqual([a[-1] for a in self.engine_calls if a[1] == "stop"], [ids[0]])

    def test_cleanup_failure_still_terminates_owned_group_and_blocks_retry(self):
        self.children = [Child(polls=100)]
        supervisor = self.supervisor(Sensors(60, 91), cool_dwell=0, hot_samples=1)
        supervisor.engine_runner = lambda *a, **k: (_ for _ in ()).throw(OSError("engine absent"))
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_CLEANUP")
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.signals[0], (4321, signal.SIGTERM))

    def test_hung_child_escalates_only_owned_group(self):
        child = Child(polls=100)
        original_wait = child.wait
        calls = []
        def wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("private command", timeout)
            return original_wait(timeout)
        child.wait = wait
        self.supervisor().stop_group(child)
        self.assertEqual(self.signals, [(4321, signal.SIGTERM), (4321, signal.SIGKILL)])

    def test_command_start_failure_is_inconclusive(self):
        supervisor = self.supervisor()
        supervisor.popen = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("private argv"))
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "ERROR_COMMAND_START")
        self.assertIsNone(self.status(supervisor)["attempts"][0]["child_exit_code"])
        self.assertTrue((supervisor.directory / "attempt-1.result.json").exists())
        self.assertFalse(self.engine_calls or self.signals)

    def test_metadata_failure_after_spawn_still_cleans_up(self):
        self.children = [Child(polls=100)]
        supervisor = self.supervisor()
        original_emit = supervisor.emit
        def emit(event, *args, **kwargs):
            if event == "attempt_started":
                raise OSError("evidence unavailable")
            return original_emit(event, *args, **kwargs)
        supervisor.emit = emit
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "ERROR_SUPERVISOR")
        self.assertTrue(self.engine_calls)
        self.assertEqual(self.signals[0], (4321, signal.SIGTERM))

    def test_term_cancellation_cleans_owned_process_and_does_not_retry(self):
        self.children = [Child(polls=100)]
        supervisor = self.supervisor()
        def wait(seconds):
            self.clock.wait(seconds)
            if self.spawns:
                supervisor.request_cancel(signal.SIGTERM)
        supervisor.wait = wait
        self.assertEqual(supervisor.run(), 143)
        self.assertEqual(self.status(supervisor)["status"], "CANCELLED")
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(self.engine_calls)

    def test_prestart_int_cancellation_never_touches_engine(self):
        supervisor = self.supervisor()
        supervisor.request_cancel(signal.SIGINT)
        self.assertEqual(supervisor.run(), 130)
        self.assertEqual(self.status(supervisor)["status"], "CANCELLED")
        self.assertFalse(self.spawns or self.engine_calls or self.signals)

    def test_timeout_is_inconclusive_without_retry(self):
        self.children = [Child(polls=100)]
        supervisor = self.supervisor(max_runtime=2)
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_TIMEOUT")
        self.assertEqual(len(self.spawns), 1)

    def test_no_overwrite_of_prior_evidence(self):
        self.supervisor().run()
        with self.assertRaises(FileExistsError):
            self.supervisor().run()
        self.assertEqual(len(self.spawns), 1)

    def test_url_and_credential_argument_redaction(self):
        argv = ["probe", "--source-url", "https://alice:password@camera/a/path-secret?x=query-secret#frag-secret",
                "--api-token=flag-secret", "--password", "next-secret", "API_KEY=env-secret",
                "--header", "Authorization: Bearer bearer-secret", "--ordinary", "value"]
        result = " ".join(guard.redact_argv(argv))
        for secret in ("alice", "password@", "path-secret", "query-secret", "frag-secret",
                       "flag-secret", "next-secret", "env-secret", "bearer-secret"):
            self.assertNotIn(secret, result)
        self.assertIn("https://camera/[REDACTED]", result)
        self.assertTrue(result.endswith("--ordinary value"))

    def test_linux_sysfs_reader_and_no_sensor_failure(self):
        root = Path(self.temp.name)
        with self.assertRaises(guard.SensorError):
            guard.read_linux_sensors(root)
        zone = root / "class/thermal/thermal_zone0"
        zone.mkdir(parents=True)
        for name, text in {"temp": "62000", "trip_point_0_type": "critical", "trip_point_0_temp": "95000"}.items():
            (zone / name).write_text(text)
        device = root / "class/hwmon/hwmon0"
        device.mkdir(parents=True)
        for name, text in {"name": "coretemp", "temp1_input": "65000", "temp1_crit": "100000"}.items():
            (device / name).write_text(text)
        readings = guard.read_linux_sensors(root)
        self.assertEqual([(r.celsius, r.critical) for r in readings], [(62, 95), (65, 100)])
        (zone / "temp").write_text("not trustworthy")
        with self.assertRaises(guard.SensorError):
            guard.read_linux_sensors(root)

    def test_cli_explicit_root_engine_command_and_safe_limits(self):
        base = ["--root", self.temp.name, "--case", "sample", "--engine", "docker", "--run-id", "r"]
        config = guard.parse_args(base + ["--", "probe", "--source-url", "rtsp://camera/x"])
        self.assertEqual(config.command, ("probe", "--source-url", "rtsp://camera/x"))
        for extra in (["--abort-ceiling", "95"], ["--critical-margin", "5"],
                      ["--sample-interval", "0"], ["--cool-dwell", "nan"]):
            with self.subTest(extra=extra), patch("sys.stderr"), self.assertRaises(SystemExit):
                guard.parse_args(base + extra + ["--", "probe"])
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            guard.parse_args(base + ["probe"])

    def test_parser_errors_do_not_echo_credentials(self):
        error = io.StringIO()
        with patch("sys.stderr", error), self.assertRaises(SystemExit):
            guard.parse_args(["--engine", "secret-token-value"])
        self.assertNotIn("secret-token-value", error.getvalue())
        self.assertIn("values withheld", error.getvalue())

    def startup_supervisor(self, sensors, phase="startup", **changes):
        changes.setdefault("startup_pause_cooling", True)
        supervisor = self.supervisor(sensors, **changes)
        spawn = supervisor.popen
        def launch(command, **kwargs):
            if phase is not None:
                Path(kwargs["env"]["SENTINEL_VALIDATION_PHASE_FILE"]).write_text(
                    json.dumps({"phase": phase}))
            return spawn(command, **kwargs)
        supervisor.popen = launch
        self.labels = {str(i) * 12: {
            "sentinel.validation_run": supervisor.config.run_id,
            "sentinel.validation_case": supervisor.config.case} for i in (1, 2)}
        self.paused = set()
        def engine(argv, **kwargs):
            self.engine_calls.append(argv)
            action, container_id = argv[1], argv[-1]
            output = ""
            if action == "ps":
                output = "\n".join(self.labels)
            elif action == "inspect":
                output = json.dumps(self.labels[container_id])
            elif action == "pause":
                self.paused.add(container_id)
            elif action == "unpause":
                self.paused.discard(container_id)
            return SimpleNamespace(returncode=0, stdout=output)
        supervisor.engine_runner = engine
        return supervisor

    def assert_restored(self, supervisor):
        self.assertFalse(self.paused)
        self.assertFalse(supervisor.group_stopped)
        self.assertFalse(supervisor.paused_containers)
        signals = [sig for _, sig in self.signals]
        self.assertLess(signals.index(signal.SIGCONT), signals.index(signal.SIGTERM))
        self.assertTrue(all(pid == 4321 for pid, _ in self.signals))

    def test_multiple_startup_pauses_preserve_attempt_and_publish_clock_before_continue(self):
        supervisor = self.startup_supervisor(
            Sensors(60, 60, 60, 99, 80, 69, 70, 68, 99, 70, 70, 70, 60), hot_samples=100)
        totals = []
        def killpg(pid, sig):
            self.signals.append((pid, sig))
            if sig == signal.SIGCONT:
                totals.append(json.loads(supervisor.attempt_path("pause-clock").read_text())["total_seconds"])
                supervisor.attempt_path("phase").write_text(json.dumps({"phase": "warmup"}))
        supervisor.killpg = killpg
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(totals, [3, 5])
        status = self.status(supervisor)
        self.assertEqual(status["startup_pause_count"], 2)
        self.assertEqual(status["startup_pause_seconds"], 5)
        self.assertEqual([p["phase"] for p in status["startup_pauses"]], ["startup", "warmup"])
        for pause in status["startup_pauses"]:
            self.assertEqual(pause["seconds"], pause["end_monotonic"] - pause["start_monotonic"])
            self.assertEqual(pause["reasons"], ["near_critical_temperature"])
            self.assertEqual(pause["container_ids"], list(self.labels))
            self.assertIn("start_utc", pause)
            self.assertIn("end_utc", pause)
        records = self.records(supervisor)
        started = next(r for r in records if r["event"] == "attempt_started")
        self.assertEqual(status["monotonic"] - started["monotonic"], 5)
        self.assert_restored(supervisor)
        self.assertFalse(any(a[1] == "stop" for a in self.engine_calls))

    def test_pause_requires_opt_in_and_valid_startup_or_warmup_phase(self):
        for index, (phase, enabled) in enumerate((
                ("startup", False), ("steady", True), ("cleanup", True),
                (None, True), ([], True), ("unknown", True))):
            with self.subTest(phase=phase, enabled=enabled):
                self.children = [Child(polls=100)]
                self.signals, self.engine_calls = [], []
                supervisor = self.startup_supervisor(
                    Sensors(60, 99), phase=phase, startup_pause_cooling=enabled,
                    cool_dwell=0, max_attempts=1, run_id=f"gate-{index}")
                self.assertEqual(supervisor.run(), 2)
                self.assertEqual(self.status(supervisor)["status"], "BLOCKED_THERMAL")
                self.assertFalse(any(a[1] in ("pause", "unpause") for a in self.engine_calls))
                self.assertNotIn((4321, signal.SIGSTOP), self.signals)

    def test_native_deadlines_forbid_freezing_even_during_startup(self):
        supervisor = self.startup_supervisor(
            Sensors(60, 99), cool_dwell=0, max_attempts=1)
        launch = supervisor.popen
        def native_launch(command, **kwargs):
            child = launch(command, **kwargs)
            Path(kwargs['env']['SENTINEL_VALIDATION_PHASE_FILE']).write_text(json.dumps({
                'phase': 'startup', 'native_clock_deadlines_active': True}))
            return child
        supervisor.popen = native_launch
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)['status'], 'BLOCKED_THERMAL')
        self.assertFalse(any(a[1] in ('pause', 'unpause') for a in self.engine_calls))
        self.assertNotIn((4321, signal.SIGSTOP), self.signals)

    def test_malformed_phase_aborts_and_steady_thermal_still_retries(self):
        for phase in ('{"phase":"steady"}', "{broken"):
            with self.subTest(phase=phase):
                self.children = [Child(polls=100), Child()]
                self.spawns, self.engine_calls = [], []
                supervisor = self.startup_supervisor(Sensors(60, 99, 60), cool_dwell=0,
                                                     run_id="steady" if phase.startswith('{"') else "malformed")
                launch = supervisor.popen
                def write_phase(command, **kwargs):
                    child = launch(command, **kwargs)
                    Path(kwargs["env"]["SENTINEL_VALIDATION_PHASE_FILE"]).write_text(phase)
                    return child
                supervisor.popen = write_phase
                self.assertEqual(supervisor.run(), 0)
                self.assertEqual(len(self.spawns), 2)
                self.assertEqual(self.status(supervisor)["attempts"][0]["reason"], "thermal")
                self.assertFalse(any(a[1] == "pause" for a in self.engine_calls))
                for number, (_, options, _) in enumerate(self.spawns, 1):
                    for key, suffix in (("SENTINEL_VALIDATION_PHASE_FILE", "phase"),
                                        ("SENTINEL_VALIDATION_PAUSE_CLOCK", "pause-clock")):
                        self.assertEqual(options["env"][key], str(supervisor.directory / f"attempt-{number}.{suffix}.json"))

    def test_pause_and_unpause_require_both_exact_labels_and_saved_ids(self):
        supervisor = self.startup_supervisor(Sensors(60, 91, 60), cool_dwell=0, hot_samples=1)
        self.labels["222222222222"].pop("sentinel.validation_case")
        self.labels["333333333333"] = {"sentinel.validation_run": "other", "sentinel.validation_case": "sample"}
        self.labels["444444444444"] = {"sentinel.validation_run": "run-1", "sentinel.validation_case": "other"}
        self.assertEqual(supervisor.run(), 0)
        for action in ("pause", "unpause"):
            self.assertEqual([a[-1] for a in self.engine_calls if a[1] == action], ["111111111111"])
        ps = next(a for a in self.engine_calls if a[1] == "ps")
        self.assertIn("label=sentinel.validation_run=run-1", ps)
        self.assertIn("label=sentinel.validation_case=sample", ps)
        self.assert_restored(supervisor)

    def test_pause_cap_blocks_without_restarting_or_seventh_pause(self):
        self.children = [Child(polls=100)]
        supervisor = self.startup_supervisor(Sensors(60, *([99, 60] * 7)), cool_dwell=0)
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_THERMAL")
        self.assertEqual(self.status(supervisor)["startup_pause_count"], 6)
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.signals.count((4321, signal.SIGSTOP)), 6)
        self.assert_restored(supervisor)

    def test_failed_cooling_restores_before_abort(self):
        for failure, expected in (("timeout", "BLOCKED_THERMAL"), ("cancel", "CANCELLED"),
                                  ("sensor", "BLOCKED_SENSOR"), ("pressure", "BLOCKED_RESOURCE_PRESSURE"),
                                  ("pressure_missing", "BLOCKED_PRESSURE_UNAVAILABLE")):
            with self.subTest(failure=failure):
                self.children = [Child(polls=100)]
                self.signals, self.engine_calls = [], []
                sample = OSError("lost sensor") if failure == "sensor" else 80
                supervisor = self.startup_supervisor(Sensors(60, 99, sample), cool_dwell=0, run_id=failure)
                if failure == "cancel":
                    def wait(seconds):
                        self.clock.wait(seconds)
                        supervisor.request_cancel(signal.SIGTERM)
                    supervisor.wait = wait
                if failure.startswith("pressure"):
                    pressure = supervisor.pressure_reader
                    def read_pressure(root):
                        if supervisor.group_stopped:
                            return {} if failure == "pressure_missing" else {
                                "available_ram_bytes": 1, "free_disk_bytes": 8 * 1024 ** 3}
                        return pressure(root)
                    supervisor.pressure_reader = read_pressure
                self.assertEqual(supervisor.run(), 143 if failure == "cancel" else 2)
                self.assertEqual(self.status(supervisor)["status"], expected)
                self.assert_restored(supervisor)
                self.assertEqual(self.status(supervisor)["startup_pause_count"], 1)

    def test_partial_engine_pause_failure_restores_all_saved_ids(self):
        supervisor = self.startup_supervisor(Sensors(60, 99), cool_dwell=0)
        engine = supervisor.engine_runner
        def fail(argv, **kwargs):
            result = engine(argv, **kwargs)
            if argv[1] == "pause" and argv[-1] == "222222222222":
                raise subprocess.TimeoutExpired("engine", 20)
            return result
        supervisor.engine_runner = fail
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_CLEANUP")
        self.assert_restored(supervisor)

    def test_unpause_failure_continues_group_and_other_containers_then_retries_cleanup(self):
        supervisor = self.startup_supervisor(Sensors(60, 99, 60), cool_dwell=0)
        engine = supervisor.engine_runner
        failed = []
        def fail(argv, **kwargs):
            if argv[1] == "unpause" and argv[-1] == "111111111111" and not failed:
                failed.append(True)
                return SimpleNamespace(returncode=1, stdout="")
            return engine(argv, **kwargs)
        supervisor.engine_runner = fail
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_CLEANUP")
        self.assert_restored(supervisor)

    def test_unpause_rechecks_ownership_even_for_saved_id(self):
        supervisor = self.startup_supervisor(Sensors(60, 99, 60), cool_dwell=0)
        engine = supervisor.engine_runner
        def relabel(argv, **kwargs):
            result = engine(argv, **kwargs)
            if argv[1] == "pause" and argv[-1] == "111111111111":
                self.labels[argv[-1]]["sentinel.validation_run"] = "other"
            return result
        supervisor.engine_runner = relabel
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_CLEANUP")
        self.assertFalse(any(a[1] in ("unpause", "stop") and a[-1] == "111111111111"
                             for a in self.engine_calls))
        self.assertIn((4321, signal.SIGCONT), self.signals)
        self.assertFalse(supervisor.group_stopped)

    def test_pause_evidence_failure_still_unfreezes_before_abort(self):
        supervisor = self.startup_supervisor(Sensors(60, 99, 60), cool_dwell=0)
        atomic = guard.atomic_json
        writes = []
        def fail(path, record):
            if path.name.endswith("pause-clock.json"):
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("evidence write failed")
            return atomic(path, record)
        with patch.object(guard, "atomic_json", side_effect=fail):
            self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "ERROR_SUPERVISOR")
        self.assert_restored(supervisor)

    def test_runtime_watchdog_includes_startup_cooling(self):
        supervisor = self.startup_supervisor(Sensors(60, 60, 60, 99, 60), max_runtime=1)
        self.assertEqual(supervisor.run(), 2)
        self.assertEqual(self.status(supervisor)["status"], "BLOCKED_TIMEOUT")
        self.assertEqual(self.status(supervisor)["startup_pause_seconds"], 1)
        self.assert_restored(supervisor)

    def test_startup_pause_cli_defaults_and_explicit_override(self):
        base = ["--root", self.temp.name, "--case", "sample", "--engine", "docker", "--run-id", "r"]
        default = guard.parse_args(base + ["--", "probe"])
        self.assertFalse(default.startup_pause_cooling)
        self.assertEqual(default.max_startup_pauses, 6)
        self.assertEqual(default.cool_dwell, 15)
        explicit = guard.parse_args(base + ["--startup-pause-cooling", "--max-startup-pauses", "12",
                                            "--cool-dwell", "3", "--max-runtime", "600", "--", "probe"])
        self.assertTrue(explicit.startup_pause_cooling)
        self.assertEqual((explicit.max_startup_pauses, explicit.cool_dwell, explicit.max_runtime), (12, 3, 600))
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            guard.parse_args(base + ["--max-startup-pauses", "0", "--", "probe"])


if __name__ == "__main__":
    unittest.main()
