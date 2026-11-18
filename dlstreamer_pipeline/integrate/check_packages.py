#!/usr/bin/env python3
"""Checks every package the stand-in agent saved against the V8 contract schemas (contract/ folder, unchanged copies).

Per package: manifest vs delivery-agent-package.schema.json, observation vs observation.schema.json, each evidence
metadata vs evidence.schema.json and its JPEG (decodes, size matches), each embedding vs embedding.schema.json plus
dimension and L2 norm. Prints counts per event type, every distinct error, and a few example observations.

Usage: check_packages.py RUN_DIR
"""
import collections
import glob
import json
import os
import sys

import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))
C = os.path.join(HERE, "contract")
run = sys.argv[1]


def open_refs(node):
    """The package schema points at observation/evidence/embedding.schema.json by file name; those parts are
    validated separately below, so here the references are left open (works with old and new jsonschema)."""
    if isinstance(node, dict):
        if node.get("$ref", "").endswith(".schema.json"):
            return {}
        return {k: open_refs(v) for k, v in node.items()}
    if isinstance(node, list):
        return [open_refs(v) for v in node]
    return node


def validator(name):
    schema = open_refs(json.load(open(os.path.join(C, name))))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


V = {k: validator(f) for k, f in (("package", "delivery-agent-package.schema.json"), ("observation", "observation.schema.json"),
                                  ("evidence", "evidence.schema.json"), ("embedding", "embedding.schema.json"))}
DIMS = {"face": 512, "body": 384, "gait": 4096, "semantic": 768}
types, errors, examples, embeds, evidence_types = collections.Counter(), collections.Counter(), {}, collections.Counter(), collections.Counter()
packages = sorted(glob.glob(os.path.join(run, "packages", "*")))
for folder in packages:
    manifest = json.load(open(os.path.join(folder, "manifest.json")))
    obs = manifest["observation"]
    kind = obs["event_type"]
    types[kind] += 1
    examples.setdefault(kind, obs)
    for key, value in (("package", manifest), ("observation", obs)):
        for e in V[key].iter_errors(value):
            errors[f"{key} {kind}: {'.'.join(map(str, e.path))}: {e.message[:120]}"] += 1
    for item in manifest["evidence"]:
        meta = item["metadata"]
        evidence_types[meta.get("evidence_type")] += 1
        for e in V["evidence"].iter_errors(meta):
            errors[f"evidence: {'.'.join(map(str, e.path))}: {e.message[:120]}"] += 1
        path = os.path.join(folder, item["part_name"] + ".jpg")
        try:
            import cv2
            image = cv2.imread(path)
            if image is None:
                errors["evidence JPEG does not decode"] += 1
            elif (image.shape[1], image.shape[0]) != (meta.get("width"), meta.get("height")):
                errors[f"evidence size {image.shape[1]}x{image.shape[0]} != metadata {meta.get('width')}x{meta.get('height')}"] += 1
        except ImportError:
            pass
    for emb in manifest["embeddings"]:
        embeds[emb.get("kind")] += 1
        for e in V["embedding"].iter_errors(emb):
            errors[f"embedding {emb.get('kind')}: {'.'.join(map(str, e.path))}: {e.message[:120]}"] += 1
        vector = emb.get("vector") or []
        if DIMS.get(emb.get("kind")) != len(vector):
            errors[f"embedding {emb.get('kind')} has {len(vector)} values, expected {DIMS.get(emb.get('kind'))}"] += 1
        norm = sum(v * v for v in vector) ** 0.5
        if abs(norm - 1.0) > 0.01:
            errors[f"embedding {emb.get('kind')} L2 norm {norm:.3f}"] += 1
print(f"packages {len(packages)} in {run}")
print("event types:", dict(types.most_common()))
print("evidence types:", dict(evidence_types), "| embeddings:", dict(embeds))
print(f"schema/contract errors: {sum(errors.values())}")
for text, n in errors.most_common(15):
    print(f"   {n} x {text}")
for kind in ("object_present", "zone_entry", "line_cross", "zone_count", "person_feature_sample", "scene_sample",
             "fire_smoke_suspected", "camera_health", "track_lost", "presence_ended_unknown"):
    if kind in examples:
        o = {k: v for k, v in examples[kind].items() if k not in ("schema_version", "deployment_id", "config_revision")}
        print(f"example {kind}: {json.dumps(o)[:400]}")
