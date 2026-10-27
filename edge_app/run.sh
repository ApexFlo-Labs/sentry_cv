#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"

export GST_PLUGIN_FEATURE_RANK=${GST_PLUGIN_FEATURE_RANK:-}
# shellcheck source=/dev/null
. /opt/intel/dlstreamer/scripts/setup_dls_env.sh

test -x .venv/bin/python || {
  echo "Missing .venv; create it with: python3 -m venv --system-site-packages .venv" >&2
  exit 1
}

exec .venv/bin/python -m edge_app.supervisor --config edge_app/config.json
