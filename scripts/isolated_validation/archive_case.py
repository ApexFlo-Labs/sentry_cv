#!/usr/bin/env python3
"""Preserve completed raw test files; sockets/devices contain no archived data."""
import argparse,hashlib,json,shutil,stat
from datetime import datetime,timezone
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--destination',type=Path,required=True);p.add_argument('--partial-reason');a=p.parse_args()
if not a.source.is_dir():p.error('source directory required')
if not (a.source/'report.json').exists() and not a.partial_reason:p.error('incomplete source requires an explicit partial reason')
a.destination.mkdir(parents=True,exist_ok=False);a.destination.chmod(0o700);files={};skipped=[]
for f in a.source.rglob('*'):
 mode=f.lstat().st_mode;dest=a.destination/f.relative_to(a.source)
 if stat.S_ISDIR(mode):dest.mkdir(parents=True,exist_ok=True)
 elif stat.S_ISREG(mode):
  dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(f,dest);files[str(f.relative_to(a.source))]=hashlib.sha256(dest.read_bytes()).hexdigest()
 else:skipped.append({'path':str(f.relative_to(a.source)),'reason':'ephemeral non-regular IPC object'})
(a.destination/'ARCHIVE_MANIFEST.json').write_text(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'source':str(a.source),'scope':'raw snapshot; no pass classification inferred','partial_reason':a.partial_reason,'files_sha256':files,'skipped':skipped},indent=2))
print(json.dumps({'archived':str(a.destination),'files':len(files),'partial_reason':a.partial_reason}))
