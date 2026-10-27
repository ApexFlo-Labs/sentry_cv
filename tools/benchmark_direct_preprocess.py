#!/usr/bin/env python3
"""Compare direct OpenVINO input/preprocessing strategies on the target GPU."""
from __future__ import annotations

import argparse
import time

import numpy as np
import openvino as ov
from openvino.preprocess import ColorFormat, PrePostProcessor


def compile_raw(core, path, requests):
    model = core.read_model(path)
    model.reshape({model.input(0): [1, 3, 640, 640]})
    return core.compile_model(model, "GPU", {
        "PERFORMANCE_HINT": "THROUGHPUT",
        "PERFORMANCE_HINT_NUM_REQUESTS": str(requests),
    })


def compile_preprocessed(core, path, requests):
    model = core.read_model(path)
    model.reshape({model.input(0): [1, 3, 640, 640]})
    ppp = PrePostProcessor(model)
    ppp.input().tensor().set_element_type(ov.Type.u8).set_layout(ov.Layout("NHWC"))
    ppp.input().tensor().set_color_format(ColorFormat.BGR)
    ppp.input().preprocess().convert_element_type(ov.Type.f32).scale(255.0)
    ppp.input().model().set_layout(ov.Layout("NCHW"))
    return core.compile_model(ppp.build(), "GPU", {
        "PERFORMANCE_HINT": "THROUGHPUT",
        "PERFORMANCE_HINT_NUM_REQUESTS": str(requests),
    })


def compile_nv12(core, path, requests):
    model = core.read_model(path)
    model.reshape({model.input(0): [1, 3, 640, 640]})
    ppp = PrePostProcessor(model)
    ppp.input().tensor().set_element_type(ov.Type.u8).set_layout(ov.Layout("NHWC"))
    ppp.input().tensor().set_color_format(ColorFormat.NV12_SINGLE_PLANE)
    ppp.input().tensor().set_spatial_static_shape(640, 640)
    ppp.input().preprocess().convert_color(ColorFormat.BGR)
    ppp.input().preprocess().convert_element_type(ov.Type.f32).scale(255.0)
    ppp.input().model().set_layout(ov.Layout("NCHW"))
    return core.compile_model(ppp.build(), "GPU", {
        "PERFORMANCE_HINT": "THROUGHPUT",
        "PERFORMANCE_HINT_NUM_REQUESTS": str(requests),
    })


def synchronous(name, compiled, tensor, iterations):
    request = compiled.create_infer_request()
    port = compiled.input(0)
    for _ in range(20):
        request.infer({port: tensor})
    started = time.perf_counter()
    for _ in range(iterations):
        request.infer({port: tensor})
    duration = time.perf_counter() - started
    print(f"{name}: {iterations / duration:.2f} FPS ({duration * 1000 / iterations:.3f} ms)")


def asynchronous(name, compiled, tensor, iterations, jobs):
    queue = ov.AsyncInferQueue(compiled, jobs)
    port = compiled.input(0)
    for _ in range(20):
        queue.start_async({port: tensor})
    queue.wait_all()
    started = time.perf_counter()
    for _ in range(iterations):
        queue.start_async({port: tensor})
    queue.wait_all()
    duration = time.perf_counter() - started
    print(f"{name}: {iterations / duration:.2f} FPS ({duration * 1000 / iterations:.3f} ms)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    core = ov.Core()
    bgr = np.full((1, 640, 640, 3), 114, dtype=np.uint8)
    nv12 = np.full((1, 960, 640, 1), 114, dtype=np.uint8)
    nchw = np.ascontiguousarray(bgr.transpose(0, 3, 1, 2), dtype=np.float32)
    nchw *= 1.0 / 255.0

    raw = compile_raw(core, args.model, 1)
    synchronous("raw-f32-nchw-sync-r1", raw, nchw, args.iterations)
    ppp = compile_preprocessed(core, args.model, 1)
    synchronous("ov-preprocess-u8-nhwc-sync-r1", ppp, bgr, args.iterations)
    for jobs in (1, 2, 4):
        async_model = compile_preprocessed(core, args.model, jobs)
        asynchronous(
            f"ov-preprocess-u8-nhwc-async-r{jobs}",
            async_model, bgr, args.iterations, jobs,
        )
    for jobs in (1, 2, 3, 4):
        nv12_model = compile_nv12(core, args.model, jobs)
        asynchronous(
            f"ov-preprocess-nv12-async-r{jobs}",
            nv12_model, nv12, args.iterations, jobs,
        )


if __name__ == "__main__":
    main()
