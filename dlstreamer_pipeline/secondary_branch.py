"""gvapython entry for his secondary analytics; the work and all shared state are in sec_shared.py.

gvapython loads this file separately for every element (without __file__), so it holds nothing
itself: the classes come from sec_shared, which a normal import keeps as one copy for the pipeline.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/chandana_optimization/cascade"))
from sec_shared import Secondary, Stash  # noqa: E402,F401
