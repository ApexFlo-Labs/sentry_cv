"""Fail closed before calling verified private method offsets (diagnostic only)."""
from pathlib import Path
import hashlib,subprocess
p=Path('/opt/intel/dlstreamer/lib/libgstvideoanalytics.so')
expected='be76f7c50ade5ad6cf22b485bd1963b79856faf504a501d0d99ceadd312851ac'
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
 raise SystemExit('unsupported SDK binary: private-method probe refused')
raise SystemExit(subprocess.run(['/checks/va-pool-wakeup-probe',str(p)],timeout=5).returncode)
