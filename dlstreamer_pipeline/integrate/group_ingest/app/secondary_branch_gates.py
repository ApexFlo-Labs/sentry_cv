"""gvapython entry for the secondary analytics with quality gates; the work is in sec_gates.py and sec_shared.py."""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/chandana_optimization/group_ingest/app"))
from sec_gates import Secondary, Stash  # noqa: E402,F401
