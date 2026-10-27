#!/usr/bin/env python3
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
VIDEOS = Path("/home/admin1/chandana_optimization/chandana_optimization_videos")
GENERATOR = Path("/home/admin1/chandana_optimization/rtsp/generate_rtsp.sh")
STATE = Path("/home/admin1/chandana_optimization/rtsp/ui_state.json")
CONFIG = Path("/home/admin1/sentry_cv/edge_app/config.json")
METRICS = Path("/run/user/1000/sentry-cv-metrics.json")
LOCK = threading.Lock()
PROCESS_CPU_SAMPLES: dict[int, tuple[int, float]] = {}
SYSTEM_CPU_SAMPLE: tuple[int, int] | None = None
CGROUP_CPU_SAMPLE: tuple[int, float] | None = None
CPU_LOCK = threading.Lock()
TELEMETRY_LOCK = threading.Lock()
TELEMETRY_CACHE: dict = {"sampled_at": 0.0}


def run(*args: str, timeout: float = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def value_int(value, default: int = 0) -> int:
    """Parse systemd/sysfs values such as '[not set]' without breaking status."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def accelerator_telemetry() -> dict:
    with TELEMETRY_LOCK:
        now = time.time()
        if now - float(TELEMETRY_CACHE.get("sampled_at", 0)) < 1.0:
            return dict(TELEMETRY_CACHE)
        npu_path = Path("/sys/class/accel/accel0/device/npu_busy_time_us")
        npu_start = read_int(str(npu_path))
        started = time.monotonic()
        result = run(
            "perf", "stat", "-x,", "-a",
            "-e", "i915/rcs0-busy/", "-e", "i915/ccs0-busy/",
            "-e", "i915/vcs0-busy/", "-e", "i915/vcs1-busy/",
            "-e", "i915/vecs0-busy/", "sleep", "0.5", timeout=3,
        )
        elapsed = time.monotonic() - started
        npu_end = read_int(str(npu_path))
        events = {}
        for line in result.stderr.splitlines():
            fields = line.split(",")
            if len(fields) < 4 or not fields[0].strip().isdigit():
                continue
            try:
                busy, event, enabled = int(fields[0]), fields[2], int(fields[3])
                events[event] = round(min(100.0, busy / enabled * 100), 2) if enabled else 0.0
            except ValueError:
                continue
        vcs0 = events.get("i915/vcs0-busy/", 0.0)
        vcs1 = events.get("i915/vcs1-busy/", 0.0)
        npu_percent = None
        if npu_start is not None and npu_end is not None and elapsed > 0:
            npu_percent = round(max(0.0, min(100.0, (npu_end - npu_start) / (elapsed * 1_000_000) * 100)), 2)
        TELEMETRY_CACHE.clear()
        TELEMETRY_CACHE.update({
            "sampled_at": now,
            "gpu_render_percent": events.get("i915/rcs0-busy/", 0.0),
            "gpu_compute_percent": events.get("i915/ccs0-busy/", 0.0),
            "gpu_decode_percent": round((vcs0 + vcs1) / 2, 2),
            "gpu_decode_0_percent": vcs0,
            "gpu_decode_1_percent": vcs1,
            "gpu_video_enhance_percent": events.get("i915/vecs0-busy/", 0.0),
            "npu_percent": npu_percent,
        })
        return dict(TELEMETRY_CACHE)


def service_properties() -> dict:
    result = run("systemctl", "--user", "show", "sentry-cv.service",
                 "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                 "-p", "NRestarts", "-p", "MemoryCurrent", "-p", "ControlGroup")
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def process_cpu(pid: int) -> float | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = int(fields[13]) + int(fields[14])
        now = time.monotonic()
        with CPU_LOCK:
            previous = PROCESS_CPU_SAMPLES.get(pid)
            PROCESS_CPU_SAMPLES[pid] = (ticks, now)
        old_ticks, old_time = previous if previous is not None else (None, None)
        if old_ticks is None or old_time is None or now <= old_time:
            return None
        return round((ticks - old_ticks) / os.sysconf("SC_CLK_TCK") / (now - old_time) * 100, 1)
    except (OSError, ValueError, IndexError):
        return None


def system_cpu() -> float | None:
    """Whole-machine CPU busy percentage from /proc/stat."""
    global SYSTEM_CPU_SAMPLE
    try:
        values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        with CPU_LOCK:
            previous = SYSTEM_CPU_SAMPLE
            SYSTEM_CPU_SAMPLE = (total, idle)
        if previous is None or total <= previous[0]:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        return round(max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100)), 1)
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return None


def cgroup_cpu(control_group: str) -> tuple[float, float] | tuple[None, None]:
    """Return service CPU as normalized percent and logical-core equivalents."""
    global CGROUP_CPU_SAMPLE
    try:
        cpu_stat = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cpu.stat"
        values = dict(line.split() for line in cpu_stat.read_text().splitlines())
        usage_usec = int(values["usage_usec"])
        now = time.monotonic()
        with CPU_LOCK:
            previous = CGROUP_CPU_SAMPLE
            CGROUP_CPU_SAMPLE = (usage_usec, now)
        if previous is None or now <= previous[1]:
            return None, None
        cores = (usage_usec - previous[0]) / 1_000_000 / (now - previous[1])
        logical = os.cpu_count() or 1
        return round(max(0.0, min(100.0, cores / logical * 100)), 1), round(max(0.0, cores), 2)
    except (OSError, ValueError, KeyError):
        return None, None


def memory() -> dict:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value, *_ = line.replace(":", "").split()
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(value) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = total - available
    return {"total": total, "used": used,
            "percent": round(used / total * 100, 1) if total else None}


def journal_health() -> dict:
    result = run("journalctl", "--user", "-u", "sentry-cv.service", "--since", "-10 min",
                 "--no-pager", timeout=5)
    text = result.stdout
    return {
        "disconnects_10m": len(re.findall(r" disconnected:", text)),
        "errors_10m": len(re.findall(r"Traceback| ERROR |failed on frame", text, re.I)),
        "last_warning": next((line.split("WARNING", 1)[-1].strip()
                              for line in reversed(text.splitlines()) if "WARNING" in line), None),
    }


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def publisher_count() -> int:
    count = 0
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = command_line.read_bytes().split(b"\0")
        except OSError:
            continue
        if (args and Path(os.fsdecode(args[0])).name == "ffmpeg"
                and any(b"rtsp://127.0.0.1:8654/" in arg for arg in args[1:])):
            count += 1
    return count


def status() -> dict:
    props = service_properties()
    pid = int(props.get("MainPID", "0") or 0)
    app_metrics = load_json(METRICS, {})
    if time.time() - float(app_metrics.get("updated_at", 0)) > 5:
        app_metrics["results_per_second"] = 0.0
        app_metrics["stale"] = True
    config = load_json(CONFIG, {"cameras": []})
    state = load_json(STATE, {})
    telemetry = accelerator_telemetry()
    logical_cpus = os.cpu_count() or 1
    sentry_percent, sentry_cores = cgroup_cpu(props.get("ControlGroup", ""))
    central_pid = value_int(app_metrics.get("central_inference", {}).get("pid"))
    inference_raw = process_cpu(central_pid) if central_pid else None
    worker_raw_values = [
        process_cpu(value_int(worker.get("pid")))
        for worker in app_metrics.get("workers", []) if value_int(worker.get("pid"))
    ]
    ingest_raw = (
        sum(value for value in worker_raw_values if value is not None)
        if any(value is not None for value in worker_raw_values) else None
    )
    enabled_cameras = [c for c in config.get("cameras", []) if c.get("enabled", True)]
    unique_urls = list(dict.fromkeys(c.get("uri") for c in enabled_cameras if c.get("uri")))
    generated_streams = state.get("streams", [])
    publisher_target = len(generated_streams) if generated_streams else len(unique_urls)
    primary_fps = config.get("settings", {}).get("fps", 0)
    smoke_fps = config.get("smoke_fire_gpu", {}).get("fps", 0)
    smoke_device = config.get("smoke_fire_gpu", {}).get("device", "GPU")
    secondary = config.get("secondary", {})
    return {
        "timestamp": time.time(),
        "service": {
            "active": props.get("ActiveState") == "active",
            "state": f"{props.get('ActiveState', 'unknown')} / {props.get('SubState', 'unknown')}",
            "pid": pid,
            "restarts": value_int(props.get("NRestarts")),
            "rss": value_int(props.get("MemoryCurrent")),
            "cpu_percent": sentry_percent,
            "cpu_cores": sentry_cores,
        },
        "cpu": {
            "logical_cpus": logical_cpus,
            "system_percent": system_cpu(),
            "sentry_percent": sentry_percent,
            "sentry_cores": sentry_cores,
            "inference_percent": (
                round(inference_raw / logical_cpus, 1) if inference_raw is not None else None
            ),
            "inference_cores": (
                round(inference_raw / 100.0, 2) if inference_raw is not None else None
            ),
            "ingest_percent": (
                round(ingest_raw / logical_cpus, 1) if ingest_raw is not None else None
            ),
            "ingest_cores": (
                round(ingest_raw / 100.0, 2) if ingest_raw is not None else None
            ),
            "metric_note": "Percentages are normalized across all logical CPUs; core values show equivalent fully utilized cores",
        },
        "memory": memory(),
        "gpu": {
            "name": "Intel Graphics (iGPU)",
            "render_mhz": read_int("/sys/class/drm/card1-HDMI-A-1/device/gt/gt0/rps_act_freq_mhz"),
            "render_max_mhz": read_int("/sys/class/drm/card1-HDMI-A-1/device/gt/gt0/rps_max_freq_mhz"),
            "media_mhz": read_int("/sys/class/drm/card1-HDMI-A-1/device/gt/gt1/rps_act_freq_mhz"),
            "media_max_mhz": read_int("/sys/class/drm/card1-HDMI-A-1/device/gt/gt1/rps_max_freq_mhz"),
            "render_percent": telemetry["gpu_render_percent"],
            "compute_percent": telemetry["gpu_compute_percent"],
            "decode_percent": telemetry["gpu_decode_percent"],
            "decode_engine_0_percent": telemetry["gpu_decode_0_percent"],
            "decode_engine_1_percent": telemetry["gpu_decode_1_percent"],
            "video_enhance_percent": telemetry["gpu_video_enhance_percent"],
            "metric_note": "Exact i915 PMU busy-time percentages sampled over 500 ms",
        },
        "npu": {
            "name": "Intel AI Boost 3720",
            "available": Path("/dev/accel/accel0").exists(),
            "active": props.get("ActiveState") == "active",
            "utilization_percent": telemetry["npu_percent"],
            "frequency_mhz": read_int("/sys/class/accel/accel0/device/npu_current_frequency_mhz"),
            "max_frequency_mhz": read_int("/sys/class/accel/accel0/device/npu_max_frequency_mhz"),
            "memory_bytes": read_int("/sys/class/accel/accel0/device/npu_memory_utilization"),
            "metric_note": "Exact intel_vpu busy-time percentage sampled over the same 500 ms",
        },
        "pipeline": app_metrics,
        "health": journal_health(),
        "streams": {
            "configured": publisher_target,
            "publishers": publisher_count(),
            "consumers": len(enabled_cameras),
            "urls": unique_urls,
            "selection": state,
        },
        "placement": [
            {"stage": "H.264/H.265 decode", "device": "iGPU / VA-API", "rate": "source FPS"},
            {"stage": "Vehicle/person inference", "device": "iGPU / OpenVINO", "rate": f"{primary_fps} FPS/stream"},
            {"stage": "Smoke/fire inference", "device": f"{smoke_device} / OpenVINO", "rate": f"{smoke_fps} FPS/stream"},
            {"stage": "Face + plate detection", "device": "NPU / OpenVINO", "rate": f"{secondary.get('input_fps', 0)} FPS/stream input"},
            {"stage": "ANPR OCR", "device": "NPU / OpenVINO", "rate": "plate-driven"},
            {"stage": "Face, ReID + gait embeddings", "device": "NPU / OpenVINO", "rate": "detection-driven"},
        ],
    }


def video_list() -> list[dict]:
    output = []
    for path in sorted(VIDEOS.glob("*")):
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi"}:
            output.append({"name": path.name, "size": path.stat().st_size})
    return output


def port_is_available(port: int) -> bool:
    state = load_json(STATE, {})
    current_port = int(state.get("port", 8654))
    if port == current_port and run("pgrep", "-x", "mediamtx").returncode == 0:
        return True
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def generate(selections: list[dict], port: int) -> dict:
    if port < 1024 or port > 65535:
        raise ValueError("RTSP port must be between 1024 and 65535")
    if not port_is_available(port):
        raise ValueError(f"Port {port} is already in use by another application")
    if not selections:
        raise ValueError("Select at least one video")
    normalized, seen, total = [], set(), 0
    for selection in selections:
        video = str(selection.get("video", ""))
        copies = int(selection.get("copies", 0))
        path = (VIDEOS / video).resolve()
        if path.parent != VIDEOS.resolve() or not path.is_file():
            raise ValueError(f"Invalid video selection: {video}")
        if video in seen:
            raise ValueError(f"Duplicate video selection: {video}")
        if copies < 1 or copies > 32:
            raise ValueError(f"Copies for {video} must be between 1 and 32")
        seen.add(video)
        total += copies
        normalized.append({"video": video, "path": str(path), "copies": copies})
    if total > 64:
        raise ValueError("A single generation is limited to 64 total streams")
    manifest_path = None
    with LOCK:
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as stream:
                json.dump({"port": port, "selections": normalized}, stream)
                manifest_path = stream.name
            result = run(str(GENERATOR), manifest_path, timeout=240)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip()[-3000:])
        finally:
            if manifest_path:
                Path(manifest_path).unlink(missing_ok=True)
    urls, index = [], 0
    for selection in normalized:
        for copy in range(1, selection["copies"] + 1):
            index += 1
            urls.append({"video": selection["video"], "copy": copy,
                         "url": f"rtsp://127.0.0.1:{port}/cam{index:02d}"})
    return {"ok": True, "port": port, "stream_count": total,
            "selections": normalized, "urls": urls, "log": result.stdout.strip()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def send_json(self, data, status_code=200):
        payload = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/videos":
            return self.send_json(video_list())
        if path == "/api/status":
            return self.send_json(status())
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/generate":
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                raise ValueError("Request is too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(generate(body.get("selections", []), int(body.get("port", 8654))))
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    host = os.getenv("SENTRY_UI_HOST", "100.121.56.73")
    port = int(os.getenv("SENTRY_UI_PORT", "8090"))
    print(f"Sentry Control listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
