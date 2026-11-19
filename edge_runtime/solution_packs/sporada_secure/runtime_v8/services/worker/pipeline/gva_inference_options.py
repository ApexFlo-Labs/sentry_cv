"""Internal compilation scheduling; never a desired-state production gate.

The tested Intel OpenCL frontend can crash during concurrent GPU compilation.
Bound compilation workers without changing inference streams, requests, device,
precision, model transforms or observation/emission policy. A low limit is a
mitigation that still requires native cold-start and sustained qualification.
"""
import json
import logging
import os
import re


def native_fire_device():
    """Explicit native hardware placement; never AUTO or a CPU fallback.

    NPU avoids the reproduced integrated GPU numerical discrepancy. This
    internal execution control is separate from desired state and event gates.
    """
    target = os.environ.get('SENTINEL_GVA_FIRE_DEVICE', 'NPU').strip().upper()
    if target not in {'GPU', 'NPU'}:
        raise ValueError('SENTINEL_GVA_FIRE_DEVICE must be GPU or NPU')
    return target


def gpu_compilation_ie_config(device):
    targets = re.split(r'[:,]',str(device).upper())
    if not any(re.fullmatch(r'GPU(?:\.\d+)*',part.strip()) for part in targets):
        return ''
    value = os.environ.get('SENTINEL_GVA_GPU_COMPILATION_THREADS','1')
    try:
        threads = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('SENTINEL_GVA_GPU_COMPILATION_THREADS requires an integer') from exc
    if not 1 <= threads <= 256:
        raise ValueError('SENTINEL_GVA_GPU_COMPILATION_THREADS must be1..256')
    logging.getLogger(__name__).info('GVA GPU compilation worker limit',extra={
        'event':'gva_gpu_compilation_configuration',
        'compilation_num_threads':threads,'target_device':str(device)})
    return 'ie-config='+json.dumps('COMPILATION_NUM_THREADS='+str(threads))+' '
