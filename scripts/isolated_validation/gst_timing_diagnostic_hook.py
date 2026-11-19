"""Trace provenance only; no model, pixel, tracker or policy monkeypatches."""
from datetime import datetime, timezone
import json
import os
import time

print(json.dumps({'event': 'validation_native_timing_trace', 'pid': os.getpid(),
                  'utc': datetime.now(timezone.utc).isoformat(),
                  'monotonic_ns': time.monotonic_ns(),
                  'scope': 'GStreamer tracer enabled; diagnostic, not capacity; runtime logic unchanged'}),
      flush=True)
