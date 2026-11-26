#!/bin/bash
# Downloads the Intel GPU driver packages listed in packages.sha256 into <out dir> and checks every sha256.
# Run on the build host (podman build itself runs with --network=none). Usage: fetch_debs.sh <out dir>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$1
BASE_URL=https://snapshot.ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu/20260916T030400Z
mkdir -p "$OUT"
grep -v '^#' "$HERE/packages.sha256" | while read -r sum path; do
  f="$OUT/$(basename "$path")"
  [ -s "$f" ] || curl -fsSL --retry 3 -o "$f" "$BASE_URL/$path"
  echo "$sum  $f" | sha256sum -c --quiet - || { echo "sha256 mismatch: $f"; rm -f "$f"; exit 1; }
done
echo "GPU driver packages ready in $OUT: $(ls "$OUT"/*.deb | wc -l)"
