#!/usr/bin/env bash
# Everything after reaches.py hucs: national reach table, stream tiles, search index,
# per-HUC8 reach CSVs, catalog/stats/HUC8 tiles, metadata. Stops at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${NFM_PY:-$HOME/miniforge3/envs/nfm-build/python.exe}"
for step in "reaches.py merge" "catalog.py" "metadata.py" "streams.py search" "streams.py csv" "streams.py tiles"; do
  echo "=== $step"
  "$PY" build/$step
done
echo "=== done"
