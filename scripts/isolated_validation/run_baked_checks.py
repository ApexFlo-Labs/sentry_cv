#!/usr/bin/env python3
"""Offline-container unit/policy checks against an explicit, locally present image.

No runtime implementation or schema is mounted. Host pytest dependencies are
copied, never installed. Both baked contract directories are validated in the
image; newdetails/sentry-v8 is validated separately by the host interpreter.
These are unit/mocked semantic checks, not native-pipeline or capacity evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
BAKED = "/opt/traffic-pilot"
DEPENDENCIES = ("pytest", "_pytest", "pluggy", "iniconfig", "packaging",
                "pygments", "py", "attrs", "attr", "exceptiongroup", "tomli")
RUN_LABEL = "sentry.baked-checks.run"
CASE_LABEL = "sentry.baked-checks.case"
PYTEST_CONFIG = "/test-support/pytest.ini"
PYTEST_CONFIG_TEXT = "[pytest]\npythonpath =\n"
SCOPE = ("Unit/mocked semantic and static contract validation only; no native "
         "pipeline, model accuracy, GPU, throughput, thermal or capacity qualification.")
PATH_CHECK = """import json, os, sys
from pathlib import Path
root = Path('/opt/traffic-pilot')
print(json.dumps({
    'parallel_background': (root/'services/worker/pipeline/parallel_background.py').is_file(),
    'validators': {p: (root/p/'validate_contract.py').is_file()
                   for p in ('image_schema', 'contracts/sentry-v8')},
    'python_version': sys.version, 'pythonpath': os.environ.get('PYTHONPATH'),
}))
"""


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize(value):
    """Keep commands replayable except for any credentials they contain."""
    value = re.sub(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1<redacted>@",
                   str(value), flags=re.I)
    return re.sub(r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key)=)[^\s&\"']+",
                  r"\1<redacted>", value)


def write_json(path, value):
    path.write_text(sanitize(json.dumps(value, indent=2, sort_keys=True)) + "\n")


def inventory(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def ignore_native(directory, names):
    return [n for n in names if n == "__pycache__" or
            n.endswith((".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib"))]


def stage_dependencies(destination):
    """Discover modules without importing pytest or installing into either system."""
    destination.mkdir()
    found = {}
    for name in DEPENDENCIES:
        spec = importlib.util.find_spec(name)
        if spec is None:
            found[name] = {"available": False}
            continue
        if spec.submodule_search_locations:
            locations = list(spec.submodule_search_locations)
            if len(locations) != 1:
                raise ValueError(f"Ambiguous dependency package: {name}")
            source = Path(locations[0])
            shutil.copytree(source, destination / name, ignore=ignore_native)
        else:
            source = Path(spec.origin or "")
            if source.suffix != ".py":
                raise ValueError(f"Dependency is not pure Python: {name}")
            shutil.copy2(source, destination / source.name)
        found[name] = {"available": True, "source": str(source)}
    missing = [n for n in DEPENDENCIES[:6] if not found[n]["available"]]
    if missing:
        raise ValueError(f"Host pytest dependencies unavailable: {', '.join(missing)}")
    return {"modules": found, "sha256": inventory(destination)}


def image_identity(stdout):
    images = json.loads(stdout)
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Expected exactly one inspected image")
    image = images[0]
    config_id = image.get("Id", image.get("ID", ""))
    if re.fullmatch(r"[0-9a-f]{64}", config_id):
        config_id = "sha256:" + config_id
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_id):
        raise ValueError("Image inspect did not return a full SHA256 config ID")
    paths = [e.split("=", 1)[1] for e in image.get("Config", {}).get("Env", []) or []
             if e.startswith("PYTHONPATH=")]
    return json.dumps({"config_id": config_id, "repo_digests": image.get("RepoDigests"),
                       "original_pythonpath": paths[-1] if paths else None}) + "\n"


def extended_pythonpath(original):
    prefix = f"/testdeps:{BAKED}:{BAKED}/services/worker"
    # Preserve the original string, including any empty path components.
    return prefix if original is None else prefix + ":" + original


def unit_arguments(has_parallel_background):
    arguments = [f"{BAKED}/tests"]
    exclusions = []
    if not has_parallel_background:
        reason = ("Baked services/worker/pipeline/parallel_background.py is absent "
                  "(known QAFT v8-r2 basedOpt79 scope difference).")
        # Pytest node IDs are relative to rootdir even with absolute input paths.
        # Gate the single repo-only test by keyword rather than an absolute ID.
        arguments += [f"--ignore={BAKED}/tests/test_parallel_background.py",
                      "-k", "not row_parallel_background_preserves_emitted_gait_packages"]
        for node in ("test_parallel_background.py",
                     "test_gait_worker.py::test_row_parallel_background_preserves_emitted_gait_packages"):
            path = f"{BAKED}/tests/{node}"
            exclusions.append({"node": path, "reason": reason})
    return arguments, exclusions


def pytest_arguments():
    # Older pytest requires a concrete inipath when coercing the pythonpath
    # setting. Explicit configuration also prevents baked/ambient config discovery.
    return ["-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", BAKED,
            "-c", PYTEST_CONFIG, "-o", "addopts="]


class Runner:
    def __init__(self, engine, output, timeout):
        self.engine, self.output, self.timeout = engine, output, timeout
        self.run_id = uuid.uuid4().hex
        self.report = {"run_id": self.run_id, "started_utc": utc(), "scope": SCOPE,
                       "engine": engine, "host_python_version": sys.version,
                       "cases": [], "exclusions": [], "result": "running"}
        self.identity = None
        self.mounts = []

    def save(self):
        write_json(self.output / "report.json", self.report)

    def execute(self, case, command, timeout=None, transform=None):
        """Record every subprocess, including inspections and scoped cleanup."""
        folder = self.output / case
        folder.mkdir()
        record = {"case": case, "argv": [sanitize(v) for v in command],
                  "command": sanitize(shlex.join(command)), "started_utc": utc(),
                  "timeout_seconds": self.timeout if timeout is None else timeout,
                  "image_config_id": self.identity["config_id"] if self.identity else None,
                  "result": "error", "returncode": None}
        write_json(folder / "command.json", record)
        started = time.monotonic()
        process = None
        stdout = folder / "stdout.txt"
        stderr = folder / "stderr.txt"
        interrupted = None
        captured = ""
        errors = ""
        try:
            with stdout.open("w") as out, stderr.open("w") as err:
                process = subprocess.Popen(command, stdout=out, stderr=err,
                                           cwd=ROOT, start_new_session=True)
                try:
                    record["returncode"] = process.wait(timeout=record["timeout_seconds"])
                    record["result"] = "passed" if process.returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    record["result"] = "timeout"
                except (KeyboardInterrupt, InterruptedError) as exc:
                    record["result"] = "interrupted"
                    interrupted = exc
                finally:
                    if process.poll() is None:
                        process.kill()  # Only this runner's subprocess, never a name/pid search.
                        process.wait(timeout=10)
                    record["returncode"] = process.returncode
            captured, errors = stdout.read_text(errors="replace"), stderr.read_text(errors="replace")
            if transform is not None:
                # Do not persist unrelated image environment variables/secrets.
                raw = captured
                captured = ""
                if record["result"] == "passed":
                    captured = transform(raw)
        except Exception as exc:
            record["result"] = "error"
            record["error"] = sanitize(str(exc))
        finally:
            stdout.write_text(sanitize(captured))
            stderr.write_text(sanitize(errors))
            record.update(ended_utc=utc(), duration_seconds=round(time.monotonic() - started, 6))
            write_json(folder / "result.json", record)
            self.report["cases"].append(record)
            self.save()
        if interrupted is not None:
            raise interrupted
        return record, captured, errors

    def container_command(self, case, arguments, mounts):
        name = f"baked-checks-{self.run_id}-{case}"
        command = [self.engine, "run", "--rm", "--pull=never", "--network=none",
                   "--no-healthcheck", "--name", name,
                   "--label", f"{RUN_LABEL}={self.run_id}",
                   "--label", f"{CASE_LABEL}={case}", "--entrypoint=python3",
                   "--workdir", BAKED,
                   "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                   "--env", "PYTEST_ADDOPTS=", "--env", "PYTEST_PLUGINS=",
                   "--env", "PYTHONDONTWRITEBYTECODE=1",
                   "--env", "OMP_NUM_THREADS=1", "--env", "OPENBLAS_NUM_THREADS=1",
                   "--env", "MKL_NUM_THREADS=1",
                   "--env", "PYTHONPATH=" + extended_pythonpath(self.identity["original_pythonpath"])]
        for source, target in mounts:
            command += ["--volume", f"{source}:{target}:ro"]
        return name, command + [self.identity["config_id"], *arguments]

    def cleanup(self, name, case):
        # Inspect exactly one generated name. Never list/filter, prune, or stop other containers.
        template = ('{"name":{{json .Name}},"run":{{json (index .Config.Labels "' +
                    RUN_LABEL + '")}},"case":{{json (index .Config.Labels "' +
                    CASE_LABEL + '")}}}')
        record, stdout, stderr = self.execute(
            case + "-cleanup-inspect",
            [self.engine, "container", "inspect", "--format", template, name], timeout=15)
        if record["returncode"] in (1, 125) and re.search(
                r"no such container|no container [^\n]* found|container [^\n]*(?:does not exist|not found)",
                stderr, re.I):
            record["result"] = "absent"
        elif record["result"] == "passed":
            try:
                owner = json.loads(stdout)
                if (owner["name"].lstrip("/") != name or owner["run"] != self.run_id or
                        owner["case"] != case):
                    raise ValueError("Refusing cleanup: exact name/run/case ownership mismatch")
            except (ValueError, KeyError, TypeError) as exc:
                record.update(result="refused", error=str(exc))
            else:
                self.execute(case + "-cleanup-remove",
                             [self.engine, "container", "rm", "--force", name], timeout=15)
        write_json(self.output / record["case"] / "result.json", record)
        self.save()

    def container_case(self, case, arguments, mounts=None):
        name, command = self.container_command(case, arguments,
                                               self.mounts if mounts is None else mounts)
        try:
            return self.execute(case, command)
        finally:
            self.cleanup(name, case)

    def prepare(self):
        self.report["dependencies"] = stage_dependencies(self.output / "testdeps")
        inputs = self.output / "inputs"
        inputs.mkdir()
        tests = ROOT / "edge_runtime/solution_packs/sporada_secure/runtime_v8/tests"
        shutil.copytree(tests, inputs / "tests", ignore=ignore_native)
        sources = [(ROOT / "scripts/isolated_validation/test_policy_scenarios.py",
                    f"{BAKED}/test_policy_scenarios.py"),
                   (ROOT / "scripts/v8_capacity_probe.py", f"{BAKED}/scripts/v8_capacity_probe.py"),
                   (ROOT / "CAPACITY_TEST_MATRIX.json", f"{BAKED}/CAPACITY_TEST_MATRIX.json")]
        self.mounts = [(self.output / "testdeps", "/testdeps"),
                       (inputs / "tests", f"{BAKED}/tests")]
        config = inputs / "pytest.ini"
        config.write_text(PYTEST_CONFIG_TEXT)
        self.mounts.append((config, PYTEST_CONFIG))
        for source, target in sources:
            snapshot = inputs / source.name
            shutil.copy2(source, snapshot)
            self.mounts.append((snapshot, target))
        self.report["inputs_sha256"] = inventory(inputs)
        self.report["canonical_host_sha256"] = inventory(ROOT / "newdetails/sentry-v8")
        self.save()

    def run(self, image):
        self.report["requested_image"] = image
        self.save()
        try:
            record, stdout, _ = self.execute(
                "image-inspect", [self.engine, "image", "inspect", image], timeout=30,
                transform=image_identity)
            if record["result"] != "passed":
                raise ValueError("Local image inspection failed; no image will be pulled")
            self.identity = json.loads(stdout)
            self.report["image"] = self.identity
            self.report["effective_pythonpath"] = extended_pythonpath(self.identity["original_pythonpath"])
            self.prepare()
            record, stdout, _ = self.container_case("baked-paths", ["-c", PATH_CHECK], mounts=[])
            if record["result"] != "passed":
                raise ValueError("Baked path inspection failed")
            paths = json.loads(stdout)
            if not isinstance(paths.get("parallel_background"), bool):
                raise ValueError("Invalid parallel-background path check")
            self.report["baked_paths"] = paths
            args, self.report["exclusions"] = unit_arguments(paths["parallel_background"])
            self.save()
            pytest = pytest_arguments()
            self.container_case("unit", [*pytest, *args])
            self.container_case("policy", [*pytest, f"{BAKED}/test_policy_scenarios.py"])
            for case, directory in (("baked-contract", "image_schema"),
                                    ("baked-canonical-contract", "contracts/sentry-v8")):
                self.container_case(case, [f"{BAKED}/{directory}/validate_contract.py"], mounts=[])
            self.execute("canonical-host-contract",
                         [sys.executable, "-B", str(ROOT / "newdetails/sentry-v8/validate_contract.py")])
            self.report["result"] = ("passed" if all(c["result"] in ("passed", "absent")
                                                     for c in self.report["cases"]) else "failed")
        except (Exception, KeyboardInterrupt) as exc:
            self.report.update(result="interrupted" if isinstance(exc, (KeyboardInterrupt, InterruptedError))
                               else "error", error=sanitize(str(exc)))
        finally:
            self.report["ended_utc"] = utc()
            self.save()
        return 0 if self.report["result"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Explicit local image reference; never pulled")
    parser.add_argument("--engine", choices=("podman", "docker"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory; existing paths refused")
    parser.add_argument("--timeout", type=float, default=300, help="Seconds per test case (default: 300)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.image.startswith("-"):
        parser.error("--image must be an image reference, not an option")
    engine = shutil.which(args.engine)
    if engine is None:
        parser.error(f"Engine executable unavailable: {args.engine}")
    output = args.output.absolute()
    if any(c in str(output) for c in (":", "\n", "\r")):
        parser.error("--output cannot contain colons or newlines (volume syntax)")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--output must not already exist")
    runner = Runner(engine, output, args.timeout)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise InterruptedError(f"Interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = runner.run(args.image)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({"result": runner.report["result"], "report": str(output / "report.json")}))
    return result


if __name__ == "__main__":
    sys.exit(main())
