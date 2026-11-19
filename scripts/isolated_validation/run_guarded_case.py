#!/usr/bin/env python3
"""Linux command supervisor. Importing this module never launches work.

Evidence is created exclusively in ROOT/RUN_ID/CASE (which must not exist).
JSON metadata is redacted; stdout/stderr are PRIVATE, UNREDACTED diagnostics.
Only thermal exclusions are retried. Exit 2 means blocked/inconclusive, never
an image failure. Containers must carry BOTH documented labels to be stopped.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit


class SensorError(RuntimeError):
    pass


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reading:
    name: str
    celsius: float
    critical: float | None = None


def read_linux_sensors(sysfs=Path("/sys")):
    """Read thermal zones and coretemp; a present but unreadable sensor fails shut.

    Critical trips are optional: without one the hard 90 C cap applies. Values
    are reread from sysfs each time; this cannot detect a stuck firmware sensor.
    """
    readings = []
    try:
        for zone in sorted((sysfs / "class/thermal").glob("thermal_zone*")):
            critical = []
            for kind in zone.glob("trip_point_*_type"):
                if kind.read_text().strip() == "critical":
                    critical.append(float(kind.with_name(
                        kind.name.replace("_type", "_temp")).read_text()) / 1000)
            readings.append(Reading(
                "thermal/" + zone.name, float((zone / "temp").read_text()) / 1000,
                min(critical) if critical else None))
        for device in sorted((sysfs / "class/hwmon").glob("hwmon*")):
            name = device / "name"
            if not name.exists() or name.read_text().strip() != "coretemp":
                continue
            inputs = sorted(device.glob("temp*_input"))
            if not inputs:
                raise SensorError("coretemp has no inputs")
            for value in inputs:
                crit = value.with_name(value.name.replace("_input", "_crit"))
                readings.append(Reading(
                    "coretemp/" + device.name + "/" + value.stem,
                    float(value.read_text()) / 1000,
                    float(crit.read_text()) / 1000 if crit.exists() else None))
    except (OSError, ValueError) as exc:
        raise SensorError("unreadable thermal sensor") from exc
    return validate_readings(readings)


def validate_readings(readings):
    readings = list(readings)
    if not readings:
        raise SensorError("no thermal sensors")
    for reading in readings:
        if (not isinstance(reading, Reading)
                or not math.isfinite(reading.celsius)
                or not 0 < reading.celsius < 150
                or (reading.critical is not None and (
                    not math.isfinite(reading.critical)
                    or not 20 <= reading.critical <= 150))):
            raise SensorError("untrustworthy thermal reading")
    return readings


def read_pressure(root):
    """Host RAM availability and free space on the evidence filesystem, in bytes."""
    info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {"available_ram_bytes": int(info["MemAvailable"].split()[0]) * 1024,
            "free_disk_bytes": shutil.disk_usage(root).free}


URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
SECRET_FLAG = re.compile(
    r"(?:pass(?:word|wd)?|token|secret|credential|api[-_]?key|access[-_]?key|"
    r"auth|cookie|header|user(?:name)?)", re.I)


def redact_text(value):
    # Even URL path components may contain bearer tokens. Keep only origin.
    def origin(match):
        try:
            url = urlsplit(match.group())
            host = url.hostname or "[REDACTED]"
            if ":" in host:
                host = "[" + host + "]"
            port = ":" + str(url.port) if url.port is not None else ""
            return url.scheme + "://" + host + port + "/[REDACTED]"
        except ValueError:
            return "[REDACTED_URL]"
    return URL.sub(origin, value)


def redact_argv(argv):
    result, hide_next = [], False
    for arg in argv:
        if hide_next:
            result.append("[REDACTED]")
            hide_next = False
        elif arg in ("-p", "-u", "-H", "-c"):
            result.append(arg)
            hide_next = True
        elif arg.startswith("-") and SECRET_FLAG.search(arg.split("=", 1)[0]):
            flag, separator, _ = arg.partition("=")
            result.append(flag + "=[REDACTED]" if separator else flag)
            hide_next = not separator
        elif re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", arg) and SECRET_FLAG.search(arg.split("=", 1)[0]):
            result.append(arg.split("=", 1)[0] + "=[REDACTED]")
        else:
            result.append(redact_text(arg))
    return result


def redacted(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    return value


def private_open(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


def atomic_json(path, record):
    temporary = path.with_name("." + path.name + ".tmp")
    with private_open(temporary) as output:
        json.dump(redacted(record), output, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class Config:
    root: Path
    case: str
    engine: str
    run_id: str
    command: tuple[str, ...]
    resume_ceiling: float = 70
    cool_dwell: float = 15
    abort_ceiling: float = 90
    critical_margin: float = 10
    hot_samples: int = 3
    sample_interval: float = 2
    max_cooldown: float = 900
    max_attempts: int = 3
    min_available_ram_mib: float = 512
    min_free_disk_mib: float = 1024
    stop_grace: float = 5
    engine_timeout: float = 20
    max_runtime: float = 0
    startup_pause_cooling: bool = False
    max_startup_pauses: int = 6


class Supervisor:
    def __init__(self, config, *, sensor_reader=read_linux_sensors,
                 pressure_reader=read_pressure, popen=subprocess.Popen,
                 engine_runner=subprocess.run, killpg=os.killpg,
                 monotonic=time.monotonic, wait=None):
        self.config = config
        self.sensor_reader, self.pressure_reader = sensor_reader, pressure_reader
        self.popen, self.engine_runner, self.killpg = popen, engine_runner, killpg
        self.clock = monotonic
        self.cancel = threading.Event()
        self.wait = wait if wait is not None else self.cancel.wait
        self.cancel_signal = signal.SIGINT
        self.attempt, self.temperatures, self.pressure = 0, [], {}
        self.attempts = []
        self.sensor_names = set()
        self.state = "INITIALIZING"
        self.directory = config.root / config.run_id / config.case
        self.events = None
        self.startup_pauses = []
        self.pause_seconds = 0.0  # Per-attempt elapsed pauses; never deducted from wall time.
        self.paused_containers = []
        self.group_stopped = False

    def attempt_path(self, suffix):
        return self.directory / f"attempt-{self.attempt}.{suffix}.json"

    def request_cancel(self, signum, _frame=None):
        # Signal handlers never launch subprocesses or perform filesystem I/O.
        self.cancel_signal = signum
        self.cancel.set()

    def emit(self, event, state=None, **fields):
        if state is not None:
            self.state = state
        record = dict(utc=datetime.now(timezone.utc).isoformat(),
                      monotonic=self.clock(), run_id=self.config.run_id,
                      case=self.config.case, engine=self.config.engine,
                      command=redact_argv(self.config.command), attempt=self.attempt,
                      temperatures=self.temperatures, pressure=self.pressure,
                      attempts=self.attempts, event=event, status=self.state)
        record.update(startup_pauses=self.startup_pauses,
                      startup_pause_count=len(self.startup_pauses),
                      startup_pause_seconds=self.pause_seconds)
        record.update(fields)
        record = redacted(record)
        self.events.write(json.dumps(record, allow_nan=False) + "\n")
        self.events.flush()
        atomic_json(self.directory / "status.json", record)
        return record

    def finish(self, status, exit_code):
        self.emit("finished", status, supervisor_exit_code=exit_code)
        return exit_code

    def sample(self):
        self.temperatures = []
        self.pressure = {}
        try:
            readings = validate_readings(self.sensor_reader())
            names = {r.name for r in readings}
            if not self.sensor_names.issubset(names):
                raise SensorError("previously observed sensor disappeared")
            self.sensor_names.update(names)
        except Exception:
            self.emit("sensor_unavailable")
            return "sensor_unavailable"
        self.temperatures = [dict(
            name=r.name, celsius=r.celsius, critical=r.critical,
            abort_ceiling=min(90, self.config.abort_ceiling,
                              r.critical - self.config.critical_margin
                              if r.critical is not None else 90)) for r in readings]
        try:
            self.pressure = self.pressure_reader(self.config.root)
            available = self.pressure["available_ram_bytes"]
            free = self.pressure["free_disk_bytes"]
            if not all(math.isfinite(v) and v >= 0 for v in (available, free)):
                raise ValueError("invalid pressure data")
        except Exception:
            self.pressure = {}
            self.emit("pressure_unavailable")
            return "pressure_unavailable"
        low_ram = available < self.config.min_available_ram_mib * 1024 ** 2
        low_disk = free < self.config.min_free_disk_mib * 1024 ** 2
        self.emit("sample", low_ram=low_ram, low_disk=low_disk)
        return "resource_pressure" if low_ram or low_disk else None

    def cool(self, deadline=None):
        self.emit("cooldown_started", "WAITING_COOLDOWN")
        start, cool_since, sensor_names = self.clock(), None, None
        while not self.cancel.is_set():
            reason = self.sample()
            if reason:
                return reason
            now = self.clock()
            if deadline is not None and now >= deadline:
                return "timeout"
            names = tuple(r["name"] for r in self.temperatures)
            if names != sensor_names:
                cool_since, sensor_names = None, names
            cool = all(r["celsius"] <= self.config.resume_ceiling
                       and r["celsius"] < r["abort_ceiling"] for r in self.temperatures)
            cool_since = (now if cool_since is None else cool_since) if cool else None
            if cool_since is not None and now - cool_since >= self.config.cool_dwell:
                self.emit("cool_dwell_satisfied")
                return None
            if now - start >= self.config.max_cooldown:
                return "thermal"
            self.wait(min(self.config.sample_interval,
                          self.config.max_cooldown - (now - start),
                          deadline - now if deadline is not None else math.inf))
        return "cancelled"

    def engine(self, *args, include_stderr=False):
        try:
            result = self.engine_runner(
                [self.config.engine, *args], capture_output=True, text=True,
                timeout=self.config.engine_timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CleanupError("engine unavailable") from exc
        if result.returncode:
            raise CleanupError("engine command failed")
        return result.stdout + (getattr(result, "stderr", "") if include_stderr else "")

    def container_owned(self, container_id):
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise CleanupError("invalid container id")
        labels = {"sentinel.validation_run": self.config.run_id,
                  "sentinel.validation_case": self.config.case}
        try:
            actual = json.loads(self.engine("inspect", "--format", "{{json .Config.Labels}}", container_id))
        except ValueError as exc:
            raise CleanupError("invalid container labels") from exc
        return isinstance(actual, dict) and all(actual.get(k) == v for k, v in labels.items())

    def container_ids(self):
        return self.engine("ps", "-q", "--filter", "label=sentinel.validation_run=" + self.config.run_id,
                           "--filter", "label=sentinel.validation_case=" + self.config.case).split()

    def stop_containers(self):
        """Both engine filters AND inspected exact label values gate every stop."""
        stopped, failed = [], False
        for container_id in self.container_ids():
            try:
                if not self.container_owned(container_id):
                    continue
                self.engine("stop", "--time", str(math.ceil(self.config.stop_grace)), container_id)
                stopped.append(container_id)
                # Capture only after stopping the load; never delay thermal
                # relief for diagnostics. A killed child cannot write its own
                # final report, and the stopped owned container retains logs.
                if self.directory.is_dir():
                    target = self.directory / f"attempt-{self.attempt}.{container_id}.stopped.log"
                    if not target.exists():
                        try:
                            data = self.engine("logs", "--tail", "3000", container_id,
                                               include_stderr=True)
                            with private_open(target) as output:
                                output.write(redact_text(data))
                            self.emit("stopped_logs_saved", container_id=container_id,
                                      path=target.name, scope="last3000lines after owned load stop")
                        except (CleanupError, OSError):
                            self.emit("stopped_log_capture_failed", container_id=container_id)
            except CleanupError:
                failed = True
        self.emit("containers_stopped", container_ids=stopped)
        if failed:
            raise CleanupError("some containers could not be inspected or stopped")

    def startup_phase(self):
        if self.config.startup_pause_cooling:
            try:
                record = json.loads(self.attempt_path("phase").read_text())
                if isinstance(record, dict) and record.get('native_clock_deadlines_active') is True:
                    return None
                if isinstance(record, dict) and record.get("phase") in ("startup", "warmup"):
                    return record["phase"]
            except (OSError, ValueError):
                pass
        return None

    def resume_owned(self, child, before_continue=None):
        """Restore every saved ID independently, then our group even if the engine fails."""
        failed = []
        for container_id in self.paused_containers:
            try:
                if not self.container_owned(container_id):
                    raise CleanupError("paused container ownership changed")
                self.engine("unpause", container_id)
            except Exception:
                failed.append(container_id)
        self.paused_containers = failed  # Abort gets another restoration attempt.
        group_ok = True
        try:
            if before_continue is not None:
                before_continue()
        finally:
            if self.group_stopped:
                try:
                    self.killpg(child.pid, signal.SIGCONT)
                    self.group_stopped = False
                except ProcessLookupError:
                    self.group_stopped = False
                except Exception:
                    group_ok = False
        return not failed and group_ok

    def pause_startup(self, child, phase, trigger, deadline):
        if len(self.startup_pauses) >= self.config.max_startup_pauses:
            self.emit("startup_pause_limit_reached")
            return "startup_pause_limit"
        pause = dict(attempt=self.attempt, phase=phase, reasons=[trigger], container_ids=[],
                     start_utc=datetime.now(timezone.utc).isoformat(), start_monotonic=self.clock())
        self.startup_pauses.append(pause)
        reason = "internal_error"
        try:
            # Freeze launchers first so they cannot advance into steady or spawn more work.
            self.group_stopped = True
            self.killpg(child.pid, signal.SIGSTOP)
            phase = self.startup_phase()
            if phase is None:
                reason = "thermal"
                return reason
            pause["phase"] = phase
            for container_id in self.container_ids():
                if self.container_owned(container_id):
                    # Save before the call: an engine timeout may still have applied pause.
                    self.paused_containers.append(container_id)
                    pause["container_ids"].append(container_id)
                    self.engine("pause", container_id)
            self.emit("startup_pause_started", "PAUSED_STARTUP_COOLING", pause=pause)
            reason = self.cool(deadline)
            if reason == "thermal":
                reason = "startup_cooldown_timeout"
            return reason
        except CleanupError:
            reason = "cleanup_failed"
            raise
        finally:
            def publish_clock():
                ended = self.clock()
                elapsed = ended - pause["start_monotonic"]
                self.pause_seconds += elapsed
                pause.update(end_utc=datetime.now(timezone.utc).isoformat(),
                             end_monotonic=ended, seconds=elapsed)
                atomic_json(self.attempt_path("pause-clock"), {"total_seconds": self.pause_seconds})

            cleanup_ok = self.resume_owned(child, before_continue=publish_clock)
            pause.update(reason=reason, cleanup_ok=cleanup_ok)
            try:
                self.emit("startup_pause_finished", "RUNNING" if reason is None and cleanup_ok else None,
                          pause=pause)
            finally:
                if not cleanup_ok:
                    raise CleanupError("startup workload could not be resumed")

    def stop_group(self, child):
        # start_new_session=True makes child.pid the process group we own.
        try:
            self.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            child.wait()
            return
        try:
            child.wait(timeout=self.config.stop_grace)
        except subprocess.TimeoutExpired:
            pass
        try:
            self.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=self.config.stop_grace)

    def abort(self, child):
        cleanup_ok = self.resume_owned(child)
        try:
            self.stop_containers()
        except Exception:
            cleanup_ok = False
        finally:
            try:
                self.stop_group(child)
            except Exception:
                cleanup_ok = False
        return cleanup_ok

    def monitor(self, child):
        started, hot_hits = self.clock(), 0
        while not self.cancel.is_set():
            reason = self.sample()
            if reason:
                return reason
            near_critical = any(r["critical"] is not None and r["celsius"] >= r["critical"] - 2
                                for r in self.temperatures)
            if near_critical:
                self.emit("near_critical_temperature")
            hot = any(r["celsius"] >= r["abort_ceiling"] for r in self.temperatures)
            hot_hits = hot_hits + 1 if hot else 0
            if near_critical or hot_hits >= self.config.hot_samples:
                phase = self.startup_phase()
                if phase is None:
                    return "thermal"
                reason = self.pause_startup(
                    child, phase, "near_critical_temperature" if near_critical else "sustained_temperature",
                    started + self.config.max_runtime if self.config.max_runtime else None)
                if reason:
                    return reason
                hot_hits = 0
                continue
            if child.poll() is not None:
                return None
            if self.config.max_runtime and self.clock() - started >= self.config.max_runtime:
                return "timeout"
            self.wait(self.config.sample_interval)
        return "cancelled"

    def blocked(self, reason):
        if reason == "cancelled":
            return self.finish("CANCELLED", 128 + self.cancel_signal)
        return self.finish({"thermal": "BLOCKED_THERMAL",
                            "startup_pause_limit": "BLOCKED_THERMAL",
                            "startup_cooldown_timeout": "BLOCKED_THERMAL",
                            "cleanup_failed": "BLOCKED_CLEANUP",
                            "sensor_unavailable": "BLOCKED_SENSOR",
                            "resource_pressure": "BLOCKED_RESOURCE_PRESSURE",
                            "pressure_unavailable": "BLOCKED_PRESSURE_UNAVAILABLE",
                            "timeout": "BLOCKED_TIMEOUT"}[reason], 2)

    def run(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(mode=0o700)  # Exclusive: never overwrite another run.
        with private_open(self.directory / "events.jsonl") as self.events:
            return self._run()

    def _run(self):
        self.emit("initialized", policy={key: value for key, value in vars(self.config).items()
                                        if key not in {"root", "command", "case", "engine", "run_id"}})
        for attempt in range(1, self.config.max_attempts + 1):
            self.attempt = attempt
            reason = self.cool()
            if reason:
                return self.blocked(reason)
            if self.cancel.is_set():
                return self.blocked("cancelled")
            env = os.environ.copy()
            env.update(SENTINEL_VALIDATION_RUN_ID=self.config.run_id,
                       SENTINEL_VALIDATION_CASE=self.config.case,
                       SENTINEL_VALIDATION_PHASE_FILE=str(self.attempt_path("phase")),
                       SENTINEL_VALIDATION_PAUSE_CLOCK=str(self.attempt_path("pause-clock")))
            self.pause_seconds = 0.0
            atomic_json(self.attempt_path("pause-clock"), {"total_seconds": 0.0})
            prefix = "attempt-" + str(attempt)
            child = None
            with private_open(self.directory / (prefix + ".stdout.log")) as stdout, \
                    private_open(self.directory / (prefix + ".stderr.log")) as stderr:
                try:
                    child = self.popen(self.config.command, stdout=stdout, stderr=stderr,
                                       env=env, start_new_session=True)
                except (OSError, ValueError):
                    summary = dict(attempt=attempt, result="ERROR_COMMAND_START",
                                   reason="command_start_failed", child_exit_code=None,
                                   stdout=prefix + ".stdout.log", stderr=prefix + ".stderr.log")
                    self.attempts.append(summary)
                    record = self.emit("command_start_failed", "ERROR_COMMAND_START", **summary)
                    atomic_json(self.directory / (prefix + ".result.json"), record)
                    return self.finish("ERROR_COMMAND_START", 2)
                reason, cleanup_ok = "internal_error", False
                try:
                    self.emit("attempt_started", "RUNNING", child_pid=child.pid)
                    reason = self.monitor(child)
                    if reason:
                        self.emit("attempt_excluded", "EXCLUDED_RESOURCE_ABORT", reason=reason)
                except CleanupError:
                    reason = "cleanup_failed"
                except Exception:
                    reason = "internal_error"
                finally:
                    if reason:
                        cleanup_ok = self.abort(child)
                    else:
                        # Reap descendants still in our group after the leader exits.
                        try:
                            self.stop_group(child)
                            cleanup_ok = True
                        except Exception:
                            cleanup_ok = False
                summary = dict(attempt=attempt, reason=reason, child_exit_code=child.returncode,
                               result="EXCLUDED_RESOURCE_ABORT" if reason else (
                                   "PASS" if child.returncode == 0 else "FAIL"),
                               cleanup_ok=cleanup_ok, stdout=prefix + ".stdout.log",
                               stderr=prefix + ".stderr.log")
                self.attempts.append(summary)
                record = self.emit("attempt_finished", **summary)
                atomic_json(self.directory / (prefix + ".result.json"), record)
                if not cleanup_ok:
                    return self.finish("BLOCKED_CLEANUP", 2)
                if self.cancel.is_set():
                    return self.blocked("cancelled")
                if reason == "internal_error":
                    return self.finish("ERROR_SUPERVISOR", 2)
                if reason == "thermal" and attempt < self.config.max_attempts:
                    continue
                if reason:
                    return self.blocked(reason)
                # Nonthermal failure is conclusive for this invocation: no autoretry.
                return self.finish("PASS" if child.returncode == 0 else "FAIL",
                                   0 if child.returncode == 0 else 1)


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse's default errors may echo credential-bearing argument values.
        super().error("invalid or missing arguments; values withheld for privacy; see --help")


def parse_args(argv=None):
    parser = PrivateArgumentParser(description=__doc__, epilog=(
        "Limits: 0 < resume-ceiling < abort-ceiling <= 90; critical-margin >= 10; "
        "positive intervals/counts; max-cooldown >= cool-dwell. "
        "IDs: 1-128 letters, digits, _, ., -; first character must be alphanumeric. "
        "Zero max-runtime means unlimited. RAM/disk thresholds use MiB."))
    parser.add_argument("--root", required=True, type=Path, help="local evidence root; cwd is unchanged")
    parser.add_argument("--case", required=True)
    parser.add_argument("--engine", required=True, choices=("podman", "docker"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--startup-pause-cooling", action="store_true",
                        help="pause owned startup/warmup workloads for cooling instead of restarting")
    for flag, default, kind in (
            ("resume-ceiling", 70, float), ("cool-dwell", 15, float),
            ("abort-ceiling", 90, float), ("critical-margin", 10, float),
            ("hot-samples", 3, int), ("sample-interval", 2, float),
            ("max-cooldown", 900, float), ("max-attempts", 3, int),
            ("max-startup-pauses", 6, int),
            ("min-available-ram-mib", 512, float), ("min-free-disk-mib", 1024, float),
            ("stop-grace", 5, float), ("engine-timeout", 20, float), ("max-runtime", 0, float)):
        parser.add_argument("--" + flag, type=kind, default=default)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- executable arguments...")
    args = parser.parse_args(argv)
    if not args.command or args.command[0] != "--" or len(args.command) == 1:
        parser.error("provide a command after --")
    args.command = tuple(args.command[1:])
    for name in ("run_id", "case"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", getattr(args, name)):
            parser.error(name + " must be a safe identifier (1-128 letters, digits, _, ., -)")
    for key, value in vars(args).items():
        if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
            parser.error(key + " must be finite and nonnegative")
    if not 0 < args.resume_ceiling < args.abort_ceiling <= 90 or args.critical_margin < 10:
        parser.error("require 0 < resume-ceiling < abort-ceiling <= 90 and critical-margin >= 10")
    if min(args.sample_interval, args.max_cooldown, args.stop_grace, args.engine_timeout,
           args.hot_samples, args.max_attempts, args.max_startup_pauses) <= 0 or args.max_cooldown < args.cool_dwell:
        parser.error("intervals/counts must be positive and max-cooldown >= cool-dwell")
    args.root = args.root.expanduser().resolve()
    return Config(**vars(args))


def main(argv=None):
    supervisor = Supervisor(parse_args(argv))
    previous = {sig: signal.signal(sig, supervisor.request_cancel)
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = supervisor.run()
        print(json.dumps({"status": supervisor.state, "exit_code": result,
                          "run_id": supervisor.config.run_id, "case": supervisor.config.case}))
        return result
    except Exception as exc:
        # Never echo raw command arguments, exception text, URLs, or diagnostics.
        print(json.dumps({"status": "ERROR_EVIDENCE_OR_SUPERVISOR",
                          "error_type": type(exc).__name__, "exit_code": 2}))
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
