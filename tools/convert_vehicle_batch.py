#!/usr/bin/env python3
"""Create a static-batch OpenVINO IR from the fixed-batch YOLO26 IR.

The learned graph and weights are retained. Only batch-fixed reshapes and the
final per-image Top-K/gather post-processing are rebuilt.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import openvino as ov
import openvino.opset13 as ops


RAW_OUTPUT_NODE = "/model.23/Transpose"
OLD_FLATTEN_NODES = {"/model.23/Flatten_1", "/model.23/Flatten"}


def make_batch_aware(source: Path, batch: int) -> ov.Model:
    core = ov.Core()
    source_model = core.read_model(source)
    patched = 0
    for node in list(source_model.get_ordered_ops()):
        if node.get_type_name() != "Reshape" or node.get_friendly_name() in OLD_FLATTEN_NODES:
            continue
        pattern_node = node.input_value(1).get_node()
        if pattern_node.get_type_name() != "Constant":
            continue
        pattern = np.asarray(pattern_node.get_vector(), dtype=np.int64)
        if pattern.size not in (3, 4) or pattern[0] != 1:
            continue
        pattern[0] = 0
        replacement = ops.reshape(
            node.input_value(0), ops.constant(pattern), True,
            name=node.get_friendly_name(),
        )
        node.output(0).replace(replacement.output(0))
        patched += 1
    if patched != 12:
        raise RuntimeError(f"expected 12 batch-fixed reshapes, patched {patched}")

    raw_nodes = [
        node for node in source_model.get_ordered_ops()
        if node.get_friendly_name() == RAW_OUTPUT_NODE
    ]
    if len(raw_nodes) != 1:
        raise RuntimeError(f"expected exactly one raw output node named {RAW_OUTPUT_NODE}")
    raw = raw_nodes[0].output(0)  # [B, 8400, 84]

    split = ops.variadic_split(
        raw, ops.constant(np.int64(2)),
        ops.constant(np.asarray([4, 80], dtype=np.int64)),
    )
    boxes, class_scores = split.output(0), split.output(1)

    # Select the 300 candidates with the highest class score per image.
    max_scores = ops.reduce_max(
        class_scores, ops.constant(np.asarray([2], dtype=np.int64)), False
    )
    first_topk = ops.topk(
        max_scores, ops.constant(np.int64(300)), -1,
        "max", "value", "i64", False,
    )
    candidate_indices = first_topk.output(1)  # [B, 300]
    candidate_indices_3d = ops.unsqueeze(
        candidate_indices, ops.constant(np.asarray([2], dtype=np.int64))
    )
    class_indices = ops.tile(
        candidate_indices_3d,
        ops.constant(np.asarray([1, 1, 80], dtype=np.int64)),
    )
    selected_class_scores = ops.gather_elements(class_scores, class_indices, 1)

    # Select the final 300 candidate/class pairs independently in every batch item.
    flattened_scores = ops.reshape(
        selected_class_scores,
        ops.constant(np.asarray([0, -1], dtype=np.int64)), True,
    )
    second_topk = ops.topk(
        flattened_scores, ops.constant(np.int64(300)), -1,
        "max", "value", "i64", False,
    )
    final_scores, flattened_indices = second_topk.output(0), second_topk.output(1)
    class_count = ops.constant(np.int64(80))
    candidate_rows = ops.divide(flattened_indices, class_count)
    selected_candidate_indices = ops.gather_elements(
        candidate_indices, candidate_rows, 1
    )
    selected_candidate_indices = ops.unsqueeze(
        selected_candidate_indices,
        ops.constant(np.asarray([2], dtype=np.int64)),
    )
    box_indices = ops.tile(
        selected_candidate_indices,
        ops.constant(np.asarray([1, 1, 4], dtype=np.int64)),
    )
    final_boxes = ops.gather_elements(boxes, box_indices, 1)
    final_scores = ops.unsqueeze(
        final_scores, ops.constant(np.asarray([2], dtype=np.int64))
    )
    class_ids = ops.floor_mod(flattened_indices, class_count)
    class_ids = ops.unsqueeze(
        class_ids, ops.constant(np.asarray([2], dtype=np.int64))
    )
    class_ids = ops.convert(class_ids, np.float32)
    output = ops.concat(
        [final_boxes, final_scores, class_ids], 2, name="output0"
    )

    model = ov.Model(
        [output], [source_model.input(0).get_node()],
        f"vehicle_yolo26n_batch{batch}",
    )
    model.reshape({model.input(0): [batch, 3, 640, 640]})
    model.validate_nodes_and_infer_types()
    expected_input = ov.PartialShape([batch, 3, 640, 640])
    expected_output = ov.PartialShape([batch, 300, 6])
    if model.input(0).partial_shape != expected_input:
        raise RuntimeError(f"unexpected input shape: {model.input(0).partial_shape}")
    if model.output(0).partial_shape != expected_output:
        raise RuntimeError(f"unexpected output shape: {model.output(0).partial_shape}")
    return model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be at least 1")
    output = args.output.with_suffix(".xml")
    output_bin = output.with_suffix(".bin")
    if not args.force and (output.exists() or output_bin.exists()):
        parser.error(f"output exists: {output} or {output_bin}; use --force to replace")
    output.parent.mkdir(parents=True, exist_ok=True)
    model = make_batch_aware(args.source, args.batch)
    ov.save_model(model, output, compress_to_fp16=False)
    saved = ov.Core().read_model(output)
    print(f"created {output} and {output_bin}")
    print(f"input={saved.input(0).partial_shape} output={saved.output(0).partial_shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
