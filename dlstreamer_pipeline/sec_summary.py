"""Median of the 'secondary:' rate lines in a run log (first two lines skipped as warm-up)."""
import re
import sys

import numpy as np

rows = [dict(re.findall(r"(\w+)=([0-9.]+)", line)) for line in open(sys.argv[1], errors="ignore")
        if line.startswith("secondary:")][2:]
if not rows:
    print("   secondary: no rate lines (branch did not run)")
else:
    med = {k: float(np.median([float(r[k]) for r in rows])) for k in rows[0]}
    print(f"   secondary per second (median of {len(rows)} x 10 s): frames {med['frames']:.1f} | plates {med['plates']:.1f} | "
          f"ANPR read {med['anpr']:.1f} | body ReID {med['reid']:.1f} | gait {med['gait']:.2f} | scene {med['scene']:.1f} | "
          f"{med['ms']:.0f} ms per frame | cameras delivering {med.get('cams', 0):.0f} | frames with no boxes {med.get('empty', 0):.1f}/s   (his: frames 13.3, plates 12.7, ANPR 0, ReID 20.3, gait 0.1, scene 1.9)")
