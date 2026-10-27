"""Supervise isolated groups of camera analytics workers."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


log = logging.getLogger(__name__)


def camera_groups(config: dict) -> list[list[str]]:
    size = int(config.get("runtime", {}).get("worker_group_size", 5))
    if size < 1:
        raise ValueError("runtime.worker_group_size must be at least 1")
    camera_ids = [
        str(camera["id"]) for camera in config["cameras"]
        if camera.get("enabled", True)
    ]
    if len(camera_ids) != len(set(camera_ids)):
        raise ValueError("camera ids must be unique")
    return [camera_ids[index:index + size] for index in range(0, len(camera_ids), size)]


class Supervisor:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.stop_requested = False
        self.workers: dict[str, subprocess.Popen] = {}
        self.groups: dict[str, list[str]] = {}
        self.restart_after: dict[str, float] = {}
        self.inference: subprocess.Popen | None = None
        self.central_smoke = False
        self.inference_restart_after = 0.0
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        self.aggregate_metrics = runtime_dir / "sentry-cv-metrics.json"
        self.primary_metrics = runtime_dir / "sentry-cv-primary.json"
        self.worker_metrics_prefix = runtime_dir / "sentry-cv-worker-"

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def load(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def start_worker(self, worker_id: str, cameras: list[str]) -> None:
        metrics = Path(f"{self.worker_metrics_prefix}{worker_id}.json")
        metrics.unlink(missing_ok=True)
        command = [
            sys.executable, "-m", "edge_app.main",
            "--config", str(self.config_path),
            "--worker-id", worker_id,
            "--camera-ids", ",".join(cameras),
            "--no-config-reload",
        ]
        self.workers[worker_id] = subprocess.Popen(command)
        self.groups[worker_id] = cameras
        log.info("started worker=%s pid=%s cameras=%s", worker_id,
                 self.workers[worker_id].pid, ",".join(cameras))

    def start_generation(self, config: dict) -> None:
        direct_enabled = str(
            config.get("settings", {}).get("primary_backend", "dlstreamer")
        ).lower() == "openvino"
        self.central_smoke = str(
            config.get("smoke_fire_gpu", {}).get("backend", "dlstreamer")
        ).lower() == "openvino"
        if direct_enabled:
            self.primary_metrics.unlink(missing_ok=True)
            self.inference = subprocess.Popen([
                sys.executable, "-m", "edge_app.direct_inference",
                "--config", str(self.config_path),
            ])
            log.info("started central inference pid=%s", self.inference.pid)
        groups = camera_groups(config)
        stagger = float(config.get("runtime", {}).get("worker_startup_stagger_seconds", 2.0))
        for index, cameras in enumerate(groups, start=1):
            if self.stop_requested:
                break
            self.start_worker(f"{index:02d}", cameras)
            if index < len(groups):
                time.sleep(max(0.0, stagger))

    def stop_generation(self) -> None:
        running = list(self.workers.items())
        for _worker_id, process in running:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 15.0
        for worker_id, process in running:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.error("worker=%s exceeded shutdown deadline; killing pid=%s",
                          worker_id, process.pid)
                process.kill()
                process.wait(timeout=5)
        self.workers.clear()
        self.groups.clear()
        self.restart_after.clear()
        if self.inference is not None:
            if self.inference.poll() is None:
                self.inference.terminate()
                try:
                    self.inference.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    log.error(
                        "central inference exceeded shutdown deadline; killing pid=%s",
                        self.inference.pid,
                    )
                    self.inference.kill()
                    self.inference.wait(timeout=5.0)
            self.inference = None
        self.central_smoke = False
        self.inference_restart_after = 0.0

    def restart_failed_workers(self, config: dict) -> None:
        backoff = float(config.get("runtime", {}).get("worker_restart_backoff_seconds", 3.0))
        now = time.monotonic()
        for worker_id, process in list(self.workers.items()):
            status = process.poll()
            if status is None:
                continue
            if worker_id not in self.restart_after:
                self.restart_after[worker_id] = now + max(0.0, backoff)
                log.error("worker=%s exited status=%s; restart scheduled", worker_id, status)
                continue
            if now >= self.restart_after[worker_id]:
                self.start_worker(worker_id, self.groups[worker_id])
                self.restart_after.pop(worker_id, None)
        if self.inference is not None and self.inference.poll() is not None:
            if not self.inference_restart_after:
                self.inference_restart_after = now + max(0.0, backoff)
                log.error(
                    "central inference exited status=%s; restart scheduled",
                    self.inference.returncode,
                )
            elif now >= self.inference_restart_after:
                self.inference = subprocess.Popen([
                    sys.executable, "-m", "edge_app.direct_inference",
                    "--config", str(self.config_path),
                ])
                self.inference_restart_after = 0.0
                log.info("restarted central inference pid=%s", self.inference.pid)

    def write_aggregate_metrics(self) -> None:
        direct_active = self.inference is not None
        primary_metrics = {}
        if direct_active:
            try:
                primary_metrics = json.loads(
                    self.primary_metrics.read_text(encoding="utf-8")
                )
                if time.time() - float(primary_metrics.get("updated_at", 0)) > 5.0:
                    primary_metrics = {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        totals = {
            "generated_results": 0,
            "primary_results": int(primary_metrics.get("primary_results", 0)),
            "smoke_fire_results": int(
                primary_metrics.get("smoke_fire_results", 0)
                if self.central_smoke else 0
            ),
            "results_per_second": float(
                primary_metrics.get("primary_results_per_second", 0.0)
            ) + float(
                primary_metrics.get("smoke_fire_results_per_second", 0.0)
                if self.central_smoke else 0.0
            ),
        }
        central_cameras = primary_metrics.get("cameras", {})
        workers = []
        for worker_id, process in sorted(self.workers.items()):
            path = Path(f"{self.worker_metrics_prefix}{worker_id}.json")
            metrics = {}
            try:
                metrics = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            if not self.central_smoke:
                totals["smoke_fire_results"] += int(metrics.get("smoke_fire_results", 0))
            if not direct_active:
                totals["primary_results"] += int(metrics.get("primary_results", 0))
            totals["results_per_second"] += float(metrics.get("results_per_second", 0.0))
            camera_metrics = metrics.get("cameras", {})
            for camera_id in self.groups[worker_id]:
                merged = dict(camera_metrics.get(camera_id, {}))
                if direct_active:
                    merged.update(central_cameras.get(camera_id, {}))
                camera_metrics[camera_id] = merged
            worker_rate = sum(
                float(values.get("primary_results_per_second", 0.0))
                + float(values.get("smoke_fire_results_per_second", 0.0))
                for values in camera_metrics.values()
            )
            workers.append({
                "id": worker_id, "pid": process.pid,
                "status": "running" if process.poll() is None else "restarting",
                "cameras": self.groups[worker_id],
                "results_per_second": round(worker_rate, 2),
                "camera_metrics": camera_metrics,
            })
        totals["generated_results"] = (
            totals["primary_results"] + totals["smoke_fire_results"]
        )
        totals["results_per_second"] = round(totals["results_per_second"], 2)
        totals["worker_count"] = len(workers)
        totals["primary_backend"] = (
            "openvino" if direct_active else "dlstreamer"
        )
        totals["central_inference"] = {
            "pid": self.inference.pid if self.inference is not None else None,
            "status": (
                "running" if self.inference is not None and self.inference.poll() is None
                else "disabled" if self.inference is None else "restarting"
            ),
            "average_inference_latency_ms": primary_metrics.get(
                "average_inference_latency_ms", 0.0
            ),
            "p95_inference_latency_ms": primary_metrics.get(
                "p95_inference_latency_ms", 0.0
            ),
        }
        totals["secondary"] = primary_metrics.get("secondary", {
            "enabled": False,
        })
        totals["workers"] = workers
        totals["updated_at"] = time.time()
        temporary = self.aggregate_metrics.with_suffix(".tmp")
        temporary.write_text(json.dumps(totals), encoding="utf-8")
        temporary.replace(self.aggregate_metrics)

    def run(self) -> int:
        config = self.load()
        self.start_generation(config)
        last_mtime = self.config_path.stat().st_mtime_ns
        try:
            while not self.stop_requested:
                time.sleep(1.0)
                if self.stop_requested:
                    break
                mtime = self.config_path.stat().st_mtime_ns
                if mtime != last_mtime:
                    try:
                        updated = self.load()
                        camera_groups(updated)  # validate before stopping healthy workers
                    except Exception:
                        log.exception("configuration reload rejected; current workers remain active")
                        last_mtime = mtime
                        continue
                    log.info("configuration changed; replacing worker generation")
                    self.stop_generation()
                    config = updated
                    self.start_generation(config)
                    last_mtime = mtime
                self.restart_failed_workers(config)
                self.write_aggregate_metrics()
        finally:
            self.stop_generation()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentry CV grouped worker supervisor")
    parser.add_argument("--config", default="edge_app/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    supervisor = Supervisor(Path(args.config).resolve())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
