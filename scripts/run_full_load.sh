#!/bin/bash
# Wait for the shard census, then stream every PF row out of the remote
# parquet shards into the local index. Resumable: re-running skips shards
# already stamped in the ingest ledger.
cd "$(dirname "$0")/.."
until [ -f reports/shard_census.json ]; do sleep 15; done
echo "=== census complete, starting remote PF ingest $(date) ==="
.venv/bin/python -m carddb ingest --source hf-remote 2>&1
echo "=== ingest finished $(date) ==="
.venv/bin/python -m carddb topics assign 2>&1
echo "=== topics assigned $(date) ==="
.venv/bin/python -m carddb stats 2>&1
