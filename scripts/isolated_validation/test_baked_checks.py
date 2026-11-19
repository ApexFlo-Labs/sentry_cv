"""Offline runner tests: every container/engine subprocess is mocked."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "baked_checks", Path(__file__).with_name("run_baked_checks.py"))
baked = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baked)
IMAGE_ID = "sha256:" + "a" * 64


class Process:
    def __init__(self, code=0, timeout=False):
        self.code, self.timeout = code, timeout
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("mock engine", timeout)
        self.returncode = -9 if self.killed else self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


class BakedChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.runner = baked.Runner("/mock/podman", self.output, 2)
        self.runner.identity = {"config_id": IMAGE_ID,
                                "original_pythonpath": "/opt/intel/dlstreamer/python::/original"}

    def test_image_identity_filters_environment_and_preserves_pythonpath(self):
        identity = json.loads(baked.image_identity(json.dumps([{
            "Id": IMAGE_ID, "RepoDigests": ["example@sha256:123"],
            "Config": {"Env": ["SECRET=private", "PYTHONPATH=/dlstreamer::/other:"]},
        }])))
        self.assertEqual(identity["config_id"], IMAGE_ID)
        self.assertEqual(identity["original_pythonpath"], "/dlstreamer::/other:")
        self.assertNotIn("private", json.dumps(identity))
        self.assertTrue(baked.extended_pythonpath(identity["original_pythonpath"]).endswith(
            ":/dlstreamer::/other:"))
        self.assertEqual(baked.extended_pythonpath(None),
                         "/testdeps:/opt/traffic-pilot:/opt/traffic-pilot/services/worker")
        with self.assertRaises(ValueError):
            baked.image_identity('[{"Id": "short"}]')

    def test_missing_module_excludes_only_two_exact_targets(self):
        args, exclusions = baked.unit_arguments(False)
        self.assertEqual(len(exclusions), 2)
        self.assertIn("--ignore=/opt/traffic-pilot/tests/test_parallel_background.py", args)
        self.assertEqual(args[args.index("-k") + 1],
                         "not row_parallel_background_preserves_emitted_gait_packages")
        self.assertFalse(any(a.startswith("--deselect") for a in args))
        future_args, future_exclusions = baked.unit_arguments(True)
        self.assertEqual(future_args, ["/opt/traffic-pilot/tests"])
        self.assertEqual(future_exclusions, [])

    def test_real_pytest_collection_excludes_relative_node_only_when_module_absent(self):
        import pytest

        tests = self.output / "tests"
        tests.mkdir()
        (tests / "test_gait_worker.py").write_text(
            "def test_row_parallel_background_preserves_emitted_gait_packages():\n"
            "    raise AssertionError('collection only; never execute')\n"
            "def test_worker_stays_selected():\n"
            "    raise AssertionError('collection only; never execute')\n")
        (tests / "test_parallel_background.py").write_text(
            "def test_parallel_module_selected():\n"
            "    raise AssertionError('collection only; never execute')\n")
        config_path = self.output / "pytest.ini"
        config_path.write_text(baked.PYTEST_CONFIG_TEXT)

        class Collection:
            def __init__(self):
                self.selected, self.deselected = [], []

            def pytest_collection_finish(self, session):
                self.selected = [item.nodeid for item in session.items]

            def pytest_deselected(self, items):
                self.deselected += [item.nodeid for item in items]

        for present in (False, True):
            collection = Collection()
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(module_present=present), \
                    patch.object(baked, "BAKED", str(self.output)), \
                    patch.object(baked, "PYTEST_CONFIG", str(config_path)), \
                    patch.dict(os.environ, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                           "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                args, _ = baked.unit_arguments(present)
                result = pytest.main([*baked.pytest_arguments()[2:], *args, "--collect-only"],
                                     plugins=[collection])
            self.assertEqual(result, 0, stdout.getvalue() + stderr.getvalue())
            target = ("tests/test_gait_worker.py::"
                      "test_row_parallel_background_preserves_emitted_gait_packages")
            if present:
                self.assertEqual(len(collection.selected), 3)
                self.assertIn(target, collection.selected)
                self.assertEqual(collection.deselected, [])
            else:
                self.assertEqual(collection.selected,
                                 ["tests/test_gait_worker.py::test_worker_stays_selected"])
                self.assertEqual(collection.deselected, [target])

    def test_stage_only_discovered_python_and_resources(self):
        package = self.output / "host-package"
        package.mkdir()
        (package / "__init__.py").write_text("# pure Python\n")
        (package / "data.txt").write_text("resource\n")
        (package / "native.so").write_text("never copied")
        (package / "__pycache__").mkdir()
        (package / "__pycache__/cache.pyc").write_bytes(b"never copied")
        shim = self.output / "py.py"
        shim.write_text("# shim\n")

        def find_spec(name):
            if name == "py":
                return SimpleNamespace(origin=str(shim), submodule_search_locations=None)
            if name in baked.DEPENDENCIES[:6]:
                return SimpleNamespace(submodule_search_locations=[str(package)])
            return None

        destination = self.output / "deps"
        with patch.object(baked.importlib.util, "find_spec", side_effect=find_spec):
            report = baked.stage_dependencies(destination)
        self.assertFalse(report["modules"]["exceptiongroup"]["available"])
        self.assertTrue((destination / "pytest/__init__.py").is_file())
        self.assertTrue((destination / "pytest/data.txt").is_file())
        self.assertTrue((destination / "py.py").is_file())
        self.assertFalse(any(p.suffix in (".so", ".pyc") for p in destination.rglob("*")))
        self.assertIn("pytest/__init__.py", report["sha256"])

    def test_container_is_pinned_offline_and_mounts_only_allowlisted_inputs(self):
        # prepare copies only fixtures/support and does not execute a subprocess.
        with patch.object(baked, "stage_dependencies", return_value={"mocked": True}):
            self.runner.prepare()
        name, command = self.runner.container_command("unit", ["-m", "pytest"], self.runner.mounts)
        self.assertIn("--network=none", command)
        self.assertIn("--pull=never", command)
        self.assertIn("--entrypoint=python3", command)
        self.assertIn("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", command)
        self.assertIn("PYTEST_ADDOPTS=", command)
        self.assertIn("PYTEST_PLUGINS=", command)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertIn("PYTHONPATH=" + baked.extended_pythonpath(
            self.runner.identity["original_pythonpath"]), command)
        self.assertEqual(command[-3:], [IMAGE_ID, "-m", "pytest"])
        self.assertIn(f"{baked.RUN_LABEL}={self.runner.run_id}", command)
        self.assertIn(f"{baked.CASE_LABEL}=unit", command)
        self.assertIn(self.runner.run_id, name)
        self.assertFalse(any(v.startswith(("--device", "--gpus", "--privileged")) for v in command))
        targets = {target for _, target in self.runner.mounts}
        self.assertEqual(targets, {"/testdeps", "/test-support/pytest.ini", "/opt/traffic-pilot/tests",
                                  "/opt/traffic-pilot/test_policy_scenarios.py",
                                  "/opt/traffic-pilot/scripts/v8_capacity_probe.py",
                                  "/opt/traffic-pilot/CAPACITY_TEST_MATRIX.json"})
        self.assertTrue(all(command[i + 1].endswith(":ro")
                            for i, value in enumerate(command) if value == "--volume"))
        self.assertEqual((self.output / "inputs/pytest.ini").read_text(), "[pytest]\npythonpath =\n")

    def test_explicit_config_parses_pythonpath_with_real_host_pytest(self):
        # Parse only; no collection, runtime imports, tests, or containers are run.
        from _pytest.config import _prepareconfig

        config_path = self.output / "pytest.ini"
        config_path.write_text(baked.PYTEST_CONFIG_TEXT)
        args = baked.pytest_arguments()[2:]
        args[args.index("-c") + 1] = str(config_path)
        args[args.index("--rootdir") + 1] = str(self.output)
        with patch.dict(os.environ, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                                    "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""}):
            config = _prepareconfig(args)
            try:
                self.assertEqual(config.inipath, config_path)
                self.assertEqual(config.getini("pythonpath"), [])
            finally:
                config._ensure_unconfigure()

    def test_timeout_records_evidence_and_removes_only_verified_owned_name(self):
        calls, processes = [], []
        name = f"baked-checks-{self.runner.run_id}-unit"

        def spawn(command, **kwargs):
            calls.append(command)
            if command[1] == "run":
                kwargs["stdout"].write("test output token=private\n")
                kwargs["stderr"].write("test diagnostic\n")
                process = Process(timeout=True)
            elif "inspect" in command:
                kwargs["stdout"].write(json.dumps({"name": "/" + name,
                    "run": self.runner.run_id, "case": "unit"}))
                process = Process()
            else:
                process = Process()
            processes.append(process)
            return process

        with patch.object(baked.subprocess, "Popen", side_effect=spawn):
            record, _, _ = self.runner.container_case("unit", ["-m", "pytest"])
        self.assertEqual(record["result"], "timeout")
        self.assertTrue(processes[0].killed)
        self.assertEqual(calls[-1], ["/mock/podman", "container", "rm", "--force", name])
        self.assertNotIn("private", (self.output / "unit/stdout.txt").read_text())
        self.assertIn("test diagnostic", (self.output / "unit/stderr.txt").read_text())
        saved = json.loads((self.output / "unit/result.json").read_text())
        self.assertEqual(saved["argv"], calls[0])
        self.assertIn("started_utc", saved)
        self.assertIn("ended_utc", saved)
        self.assertGreaterEqual(saved["duration_seconds"], 0)

    def test_cleanup_refuses_any_name_or_label_mismatch(self):
        name = f"baked-checks-{self.runner.run_id}-unit"
        for field in ("name", "run", "case"):
            owner = {"name": name, "run": self.runner.run_id, "case": "unit"}
            owner[field] = "someone-else"

            def execute(case, command, **kwargs):
                (self.output / case).mkdir(exist_ok=True)
                record = {"case": case, "result": "passed", "returncode": 0}
                return record, json.dumps(owner), ""

            with self.subTest(field=field), patch.object(self.runner, "execute", side_effect=execute) as run:
                self.runner.cleanup(name, "unit")
                self.assertEqual(run.call_count, 1)
                record = json.loads((self.output / "unit-cleanup-inspect/result.json").read_text())
                self.assertEqual(record["result"], "refused")

    def test_engine_connection_error_is_not_treated_as_absent_container(self):
        def spawn(command, **kwargs):
            kwargs["stderr"].write("Cannot connect: engine socket not found\n")
            return Process(125)

        with patch.object(baked.subprocess, "Popen", side_effect=spawn) as run:
            self.runner.cleanup(f"baked-checks-{self.runner.run_id}-unit", "unit")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.runner.report["cases"][-1]["result"], "failed")

    def test_mock_campaign_keeps_validators_unmounted_and_reports_failures(self):
        calls = []

        def spawn(command, **kwargs):
            calls.append(command)
            code = 0
            if command[1:3] == ["image", "inspect"]:
                kwargs["stdout"].write(json.dumps([{"Id": IMAGE_ID, "Config": {
                    "Env": ["PYTHONPATH=/dlstreamer", "SECRET=never-record-me"]}}]))
            elif command[1:3] == ["container", "inspect"]:
                code = 1
                kwargs["stderr"].write("Error: no such container\n")
            elif "--name" in command:
                name = command[command.index("--name") + 1]
                if name.endswith("-baked-paths"):
                    kwargs["stdout"].write(json.dumps({"parallel_background": True, "validators": {}}))
                elif name.endswith("-policy"):
                    code = 1
            return Process(code)

        with patch.dict(os.environ, {"PYTEST_ADDOPTS": "--invalid-ambient-option"}), \
                patch.object(baked, "stage_dependencies", return_value={}), \
                patch.object(baked.subprocess, "Popen", side_effect=spawn):
            result = self.runner.run("local:explicit")
        self.assertEqual(result, 1)
        self.assertEqual(self.runner.report["result"], "failed")
        self.assertEqual(self.runner.report["exclusions"], [])
        for command in calls:
            if any(v.endswith("/validate_contract.py") for v in command):
                self.assertNotIn("--volume", command)
            if command[1] == "run":
                self.assertIn(IMAGE_ID, command)
                self.assertNotIn("local:explicit", command)
                self.assertIn("PYTEST_ADDOPTS=", command)
                self.assertNotIn("--invalid-ambient-option", command)
                if "pytest" in command:
                    self.assertEqual(command[command.index("-c") + 1], "/test-support/pytest.ini")
                    self.assertNotIn("pythonpath=", command)
        self.assertEqual(len([c for c in calls if c[1] == "run"]), 5)
        self.assertTrue(any(c[-1] == str(baked.ROOT / "newdetails/sentry-v8/validate_contract.py")
                            for c in calls))
        self.assertNotIn("never-record-me", (self.output / "image-inspect/stdout.txt").read_text())
        self.assertTrue((self.output / "canonical-host-contract/result.json").is_file())

    def test_existing_output_is_refused_before_any_subprocess(self):
        with patch.object(baked.shutil, "which", return_value="/mock/podman"), \
                patch.object(baked.subprocess, "Popen") as spawn, \
                self.assertRaises(SystemExit):
            baked.main(["--image", "local:explicit", "--engine", "podman",
                        "--output", str(self.output)])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
